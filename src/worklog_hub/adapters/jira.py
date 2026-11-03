from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from worklog_hub.config import JiraConfig
from worklog_hub.domain import (
    CatalogProject,
    CatalogSnapshot,
    CatalogWorkItem,
    Person,
    Project,
    TimeEntry,
    WorkItem,
)
from worklog_hub.security import redact_secrets

API = "/rest/api/2"
Transport = Callable[[str, dict[str, Any]], Any]


class JiraError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Запрещаем пересылку PAT на SSO или другой сервер.
        raise JiraError(
            "Jira вернула редирект. Проверьте URL и доступ к REST API с PAT"
        )


class JiraRESTClient:
    """Только чтение штатного Server/Data Center REST API v2 через PAT."""

    def __init__(self, config: JiraConfig, transport: Transport | None = None) -> None:
        self.config = config
        self._transport = transport or self._http_get

    def get(self, resource: str, params: dict[str, Any] | None = None) -> Any:
        if not resource.startswith(API + "/") or ".." in resource or "?" in resource:
            raise JiraError("Недопустимый REST-маршрут Jira")
        try:
            return self._transport(resource, params or {})
        except Exception as exc:
            message = redact_secrets(str(exc), (self.config.token,))
            raise JiraError(f"Jira: {message}") from exc

    def _http_get(self, resource: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode(params)
        url = (
            self.config.base_url.rstrip("/") + resource + ("?" + query if query else "")
        )
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
                "User-Agent": "worklog-hub/0.1",
            },
            method="GET",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                if "json" not in response.headers.get("Content-Type", "").lower():
                    raise JiraError(
                        "Вместо JSON получена страница. Возможен вход через SSO"
                    )
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hints = {
                401: "PAT не принят",
                403: "нет прав или REST API запрещён",
                404: "ресурс не найден или недоступен",
            }
            raise JiraError(
                f"HTTP {exc.code}: {hints.get(exc.code, 'ошибка сервера')}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JiraError("Некорректный JSON в ответе") from exc


class JiraCollector:
    source_name = "jira"
    reconcile_missing = False

    def __init__(self, client: JiraRESTClient) -> None:
        self.client = client
        self._projects: dict[str, dict[str, Any]] | None = None
        self._issues: list[dict[str, Any]] | None = None

    def check(self) -> str:
        user = self.client.get(API + "/myself")
        if not isinstance(user, dict) or not (user.get("name") or user.get("key")):
            raise JiraError("Не получен текущий пользователь Jira")
        return str(user.get("name") or user["key"])

    def list_projects(self) -> list[dict[str, Any]]:
        projects = self.client.get(API + "/project")
        if not isinstance(projects, list):
            raise JiraError("Ожидался список проектов Jira")
        for project in projects:
            _project(project)
        return sorted(projects, key=lambda project: project["key"])

    def _selected_projects(self) -> dict[str, dict[str, Any]]:
        if self._projects is not None:
            return self._projects
        if not self.client.config.project_keys:
            raise JiraError("Не заданы jira.projects для синхронизации")
        projects = {}
        for key in self.client.config.project_keys:
            resource = API + "/project/" + urllib.parse.quote(key, safe="")
            project = self.client.get(resource)
            _project(project)
            if project["key"] != key:
                raise JiraError(f"Jira вернула другой ключ проекта вместо {key}")
            projects[str(project["id"])] = project
        self._projects = projects
        return projects

    def _collect_issues(self) -> list[dict[str, Any]]:
        if self._issues is not None:
            return self._issues
        projects = self._selected_projects()
        # Ищем все задачи: фильтр worklogDate может пропустить исправленные
        # списания, перенесённые на другую дату.
        keys = ", ".join(json.dumps(project["key"]) for project in projects.values())
        jql = f"project IN ({keys}) ORDER BY key ASC"
        start = 0
        total: int | None = None
        issues: dict[str, dict[str, Any]] = {}
        while True:
            page = self.client.get(
                API + "/search",
                {
                    "jql": jql,
                    "startAt": start,
                    "maxResults": 100,
                    "fields": "summary,project,issuetype,status,description,"
                    "created,updated",
                },
            )
            if not isinstance(page, dict) or not isinstance(page.get("issues"), list):
                raise JiraError("В ответе поиска Jira нет списка issues")
            page_total = _count(page.get("total"))
            if _count(page.get("startAt")) != start:
                raise JiraError("Jira не соблюдает startAt поиска")
            if total is not None and total != page_total:
                raise JiraError(
                    "Список задач изменился во время пагинации; повторите запуск"
                )
            total = page_total
            nodes = page["issues"]
            if start + len(nodes) > total or (not nodes and start < total):
                raise JiraError("Неполная или некорректная страница поиска Jira")
            for issue in nodes:
                if (
                    not isinstance(issue, dict)
                    or not issue.get("id")
                    or not issue.get("key")
                ):
                    raise JiraError("Некорректная задача Jira")
                fields = issue.get("fields")
                if not isinstance(fields, dict) or not isinstance(
                    fields.get("project"), dict
                ):
                    raise JiraError("Нет проекта в задаче Jira")
                project_id = str(fields["project"].get("id"))
                if project_id not in projects:
                    raise JiraError("Поиск Jira вернул задачу вне выбранных проектов")
                if not isinstance(fields.get("summary"), str):
                    raise JiraError("Нет названия задачи Jira")
                for name in ("description", "created", "updated"):
                    if fields.get(name) is not None and not isinstance(
                        fields[name], str
                    ):
                        raise JiraError(f"Некорректное поле задачи Jira: {name}")
                if fields.get("status") is not None and not isinstance(
                    fields["status"], dict
                ):
                    raise JiraError("Некорректный статус задачи Jira")
                _kind(fields)
                if str(issue["id"]) in issues:
                    raise JiraError("Повтор задачи в пагинации Jira; повторите запуск")
                issues[str(issue["id"])] = issue
            start += len(nodes)
            if start == total:
                break
        self._issues = list(issues.values())
        return self._issues

    def collect(self, date_from: date, date_to: date) -> list[TimeEntry]:
        if date_from > date_to:
            raise ValueError("Начальная дата позже конечной")
        entries: dict[str, TimeEntry] = {}
        projects = self._selected_projects()
        for issue in self._collect_issues():
            resource = (
                API
                + "/issue/"
                + urllib.parse.quote(str(issue["id"]), safe="")
                + "/worklog"
            )
            page = self.client.get(resource)
            if not isinstance(page, dict) or not isinstance(page.get("worklogs"), list):
                raise JiraError("В ответе Jira отсутствует worklogs")
            # Server/DC возвращает все worklog, без параметров
            # startAt/maxResults (проверено по сигнатуре IssueResource 10.3.9).
            if _count(page.get("startAt")) != 0 or _count(page.get("total")) != len(
                page["worklogs"]
            ):
                raise JiraError(
                    "Получен неполный список worklog; данные не будут записаны"
                )
            project = projects[str(issue["fields"]["project"]["id"])]
            for raw in page["worklogs"]:
                entry = normalize_worklog(
                    raw, issue, project, self.client.config.base_url
                )
                if entry.external_id in entries:
                    raise JiraError("Повтор ID worklog в Jira")
                if date_from <= entry.spent_date <= date_to:
                    entries[entry.external_id] = entry
        return list(entries.values())

    def collect_catalog(self) -> CatalogSnapshot:
        projects = self._selected_projects()
        items = []
        for issue in self._collect_issues():
            fields = issue["fields"]
            kind = _kind(fields)
            items.append(
                CatalogWorkItem(
                    external_id=str(issue["id"]),
                    kind=kind,
                    iid=str(issue["key"]),
                    title=str(fields["summary"]),
                    description=fields.get("description"),
                    state=(fields.get("status") or {}).get("name"),
                    web_url=self.client.config.base_url
                    + "/browse/"
                    + str(issue["key"]),
                    project_external_id=str(fields["project"]["id"]),
                    group_external_id=None,
                    branch_name=None,
                    created_at=fields.get("created"),
                    updated_at=fields.get("updated"),
                    raw=issue,
                )
            )
        return CatalogSnapshot(
            groups=[],
            projects=[
                CatalogProject(
                    str(p["id"]),
                    p["name"],
                    p["key"],
                    None,
                    None,
                    None,
                    bool(p.get("archived", False)),
                    p,
                )
                for p in projects.values()
            ],
            memberships=[],
            work_items=items,
            branches=[],
            iterations=[],
            warnings=[
                "Jira: исчезнувшие записи сохраняются; "
                "пустой worklog не доказывает удаление"
            ],
            incomplete_tables={
                "groups",
                "projects",
                "users",
                "memberships",
                "work_items",
                "branches",
                "iterations",
            },
        )


def _project(raw: Any) -> None:
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(key), str) and raw[key] for key in ("id", "key", "name")
    ):
        raise JiraError("Некорректный проект Jira")


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JiraError("Некорректные метаданные пагинации Jira")
    return value


def _kind(fields: dict[str, Any]) -> str:
    issue_type = fields.get("issuetype") or {}
    if not isinstance(issue_type, dict):
        raise JiraError("Некорректный тип задачи Jira")
    if str(issue_type.get("name", "")).casefold() in {"epic", "эпик"}:
        return "epic"
    return "subtask" if issue_type.get("subtask") else "issue"


def normalize_worklog(
    raw: dict[str, Any],
    issue: dict[str, Any],
    project: dict[str, Any],
    base_url: str,
) -> TimeEntry:
    try:
        parsed = datetime.fromisoformat(str(raw["started"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("started не содержит часовой пояс")
        if raw.get("issueId") is not None and str(raw["issueId"]) != str(issue["id"]):
            raise ValueError("worklog относится к другой задаче")
        external_id = str(raw["id"])
        author = raw["author"]
        user_id = author.get("key") or author.get("name")
        if not user_id or not external_id or external_id == "None":
            raise ValueError("нет ID автора или worklog")
        comment = raw.get("comment")
        if comment is not None and not isinstance(comment, str):
            raise ValueError("Ожидался текст комментария Server/Data Center")
        return TimeEntry(
            external_id=external_id,
            user=Person(
                str(user_id),
                str(author.get("name") or user_id),
                author.get("displayName"),
            ),
            project=Project(str(project["id"]), project["name"], project["key"]),
            work_item=WorkItem(
                str(issue["id"]),
                _kind(issue["fields"]),
                str(issue["fields"]["summary"]),
                str(issue["key"]),
                base_url + "/browse/" + str(issue["key"]),
            ),
            spent_at=parsed.astimezone(UTC),
            spent_date=parsed.date(),
            seconds=_count(raw["timeSpentSeconds"]),
            summary=comment,
            raw=raw,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise JiraError("Некорректный worklog Jira") from exc
