from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from worklog_hub.config import GitLabConfig
from worklog_hub.domain import Person, Project, TimeEntry, WorkItem
from worklog_hub.security import redact_secrets

CHECK_QUERY = """
query WorklogHubCheck {
  currentUser { id username name }
}
"""

TIMELOGS_QUERY = """
query WorklogHubTimelogs(
  $startDate: Time!
  $endDate: Time!
  $groupId: GroupID
  $projectId: ProjectID
  $after: String
) {
  timelogs(
    startDate: $startDate
    endDate: $endDate
    groupId: $groupId
    projectId: $projectId
    first: 100
    after: $after
  ) {
    nodes {
      id
      spentAt
      timeSpent
      summary
      user { id username name webUrl }
      project { id name fullPath webUrl }
      issue { id iid title webUrl }
      mergeRequest { id iid title webUrl sourceBranch }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

TIMELOG_GROUPS_QUERY = """
query WorklogHubTimelogGroups($after: String) {
  groups(allAvailable: false, first: 100, after: $after) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""

TIMELOG_PROJECTS_QUERY = """
query WorklogHubTimelogProjects($after: String) {
  projects(membership: true, first: 100, after: $after) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class GitLabError(RuntimeError):
    pass


class GitLabGraphQLError(GitLabError):
    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        self.graphql_message = message
        super().__init__(f"GitLab GraphQL ({operation}) вернул ошибку: {message}")

    @property
    def catalog_resource_unavailable(self) -> bool:
        message = self.graphql_message.casefold()
        return any(
            marker in message
            for marker in (
                "does not exist or you don't have permission",
                "access denied",
                "not authorized",
                "doesn't exist on type",
                "unknown argument",
                "is not defined by type",
            )
        )


class GitLabResourceUnavailable(GitLabError):
    pass


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]
GRAPHQL_OPERATION_RE = re.compile(r"\b(?:query|mutation)\s+([_A-Za-z][_0-9A-Za-z]*)")


class GitLabGraphQLClient:
    def __init__(
        self, config: GitLabConfig, transport: Transport | None = None
    ) -> None:
        self.config = config
        self._transport = transport or self._http_post

    def execute(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        operation = _operation_name(query)
        try:
            response = self._transport(query, variables or {})
        except GitLabError:
            raise
        except Exception as exc:
            message = redact_secrets(str(exc), (self.config.token,))
            raise GitLabError(f"Ошибка обращения к GitLab: {message}") from exc
        errors = response.get("errors")
        if errors:
            rendered = "; ".join(str(item.get("message", item)) for item in errors)
            raise GitLabGraphQLError(
                operation,
                redact_secrets(rendered, (self.config.token,)),
            )
        data = response.get("data")
        if not isinstance(data, dict):
            raise GitLabError("GitLab GraphQL не вернул объект data")
        return data

    def _http_post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self.config.graphql_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.token}",
                "User-Agent": "worklog-hub/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            safe = redact_secrets(detail, (self.config.token,))
            raise GitLabError(f"GitLab HTTP {exc.code}: {safe}") from exc
        except urllib.error.URLError as exc:
            raise GitLabError(f"GitLab недоступен: {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitLabError("GitLab вернул некорректный JSON") from exc


class GitLabCollector:
    source_name = "gitlab"

    def __init__(self, client: GitLabGraphQLClient) -> None:
        self.client = client

    def check(self) -> str:
        user = self.client.execute(CHECK_QUERY).get("currentUser")
        if not isinstance(user, dict) or not user.get("username"):
            raise GitLabError("Подключение установлено, но currentUser недоступен")
        return str(user["username"])

    def collect(self, date_from: date, date_to: date) -> list[TimeEntry]:
        if date_from > date_to:
            raise ValueError("Начальная дата позже конечной")
        scopes = [
            ("groupId", external_id)
            for external_id in self._collect_scope_ids(TIMELOG_GROUPS_QUERY, "groups")
        ]
        scopes.extend(
            ("projectId", external_id)
            for external_id in self._collect_scope_ids(
                TIMELOG_PROJECTS_QUERY, "projects"
            )
        )
        entries_by_id: dict[str, TimeEntry] = {}
        for scope_name, scope_id in scopes:
            for entry in self._collect_scope(
                date_from,
                date_to,
                scope_name=scope_name,
                scope_id=scope_id,
            ):
                entries_by_id[entry.external_id] = entry
        return list(entries_by_id.values())

    def _collect_scope(
        self,
        date_from: date,
        date_to: date,
        *,
        scope_name: str,
        scope_id: str,
    ) -> list[TimeEntry]:
        cursor: str | None = None
        result: list[TimeEntry] = []
        seen_cursors: set[str] = set()

        while True:
            variables = {
                "startDate": date_from.isoformat(),
                "endDate": date_to.isoformat(),
                "groupId": scope_id if scope_name == "groupId" else None,
                "projectId": scope_id if scope_name == "projectId" else None,
                "after": cursor,
            }
            connection = self.client.execute(TIMELOGS_QUERY, variables).get("timelogs")
            if not isinstance(connection, dict):
                raise GitLabError("В ответе отсутствует timelogs")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise GitLabError("В ответе отсутствует timelogs.nodes")
            result.extend(normalize_timelog(node) for node in nodes)

            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            next_cursor = page_info.get("endCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise GitLabError("GitLab вернул некорректный курсор пагинации")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return result

    def _collect_scope_ids(self, query: str, connection_key: str) -> list[str]:
        cursor: str | None = None
        result: list[str] = []
        seen_cursors: set[str] = set()
        while True:
            connection = self.client.execute(query, {"after": cursor}).get(
                connection_key
            )
            if not isinstance(connection, dict):
                raise GitLabError(f"В ответе отсутствует {connection_key}")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise GitLabError(f"В ответе отсутствует {connection_key}.nodes")
            result.extend(
                str(node["id"])
                for node in nodes
                if isinstance(node, dict) and node.get("id")
            )
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return result
            next_cursor = page_info.get("endCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise GitLabError(
                    f"GitLab вернул некорректный курсор для {connection_key}"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor


def normalize_timelog(raw: dict[str, Any]) -> TimeEntry:
    try:
        spent_at_raw = str(raw["spentAt"])
        parsed = datetime.fromisoformat(spent_at_raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("spentAt не содержит часовой пояс")
        spent_date = parsed.date()
        spent_at = parsed.astimezone(UTC)
        user_raw = raw["user"]
        project_raw = raw["project"]
        item_raw = raw.get("issue") or raw.get("mergeRequest")
        item_kind = "issue" if raw.get("issue") else "merge_request"
        work_item = None
        if item_raw:
            work_item = WorkItem(
                external_id=str(item_raw["id"]),
                kind=item_kind,
                iid=_optional_string(item_raw.get("iid")),
                title=str(item_raw["title"]),
                web_url=_optional_string(item_raw.get("webUrl")),
                branch_name=_optional_string(item_raw.get("sourceBranch")),
            )
        return TimeEntry(
            external_id=str(raw["id"]),
            user=Person(
                external_id=str(user_raw["id"]),
                username=str(user_raw["username"]),
                display_name=_optional_string(user_raw.get("name")),
                web_url=_optional_string(user_raw.get("webUrl")),
            ),
            project=Project(
                external_id=str(project_raw["id"]),
                name=str(project_raw["name"]),
                full_path=_optional_string(project_raw.get("fullPath")),
                web_url=_optional_string(project_raw.get("webUrl")),
            ),
            work_item=work_item,
            spent_at=spent_at,
            spent_date=spent_date,
            seconds=int(raw["timeSpent"]),
            summary=_optional_string(raw.get("summary")),
            raw=raw,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitLabError(f"Некорректный timelog: {exc}") from exc


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _operation_name(query: str) -> str:
    match = GRAPHQL_OPERATION_RE.search(query)
    return match.group(1) if match else "anonymous"
