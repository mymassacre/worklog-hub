from __future__ import annotations

from typing import Any

from worklog_hub.adapters.gitlab import GitLabError, GitLabGraphQLClient
from worklog_hub.domain import (
    CatalogBranch,
    CatalogGroup,
    CatalogIteration,
    CatalogMembership,
    CatalogProject,
    CatalogSnapshot,
    CatalogUser,
    CatalogWorkItem,
)

GROUPS_QUERY = """
query WorklogHubGroups($after: String) {
  groups(allAvailable: false, first: 100, after: $after) {
    nodes { id name fullPath webUrl parent { id } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PROJECTS_QUERY = """
query WorklogHubProjects($after: String) {
  projects(membership: true, first: 100, after: $after) {
    nodes {
      id name fullPath webUrl archived
      namespace { id }
      repository { rootRef }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

GROUP_MEMBERS_QUERY = """
query WorklogHubGroupMembers($fullPath: ID!, $after: String) {
  group(fullPath: $fullPath) {
    groupMembers(first: 100, after: $after) {
      nodes {
        id expiresAt
        accessLevel { stringValue integerValue }
        user { id username name webUrl }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

GROUP_ITERATIONS_QUERY = """
query WorklogHubGroupIterations($fullPath: ID!, $after: String) {
  group(fullPath: $fullPath) {
    iterations(
      first: 100
      after: $after
      includeAncestors: false
      includeDescendants: false
    ) {
      nodes {
        id iid title description state startDate dueDate webUrl
        group { id }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

GROUP_EPICS_QUERY = """
query WorklogHubGroupEpics($fullPath: ID!, $after: String) {
  group(fullPath: $fullPath) {
    workItems(
      types: [EPIC]
      excludeProjects: true
      includeDescendants: false
      first: 100
      after: $after
    ) {
      nodes {
        id iid title description state createdAt updatedAt webUrl
        namespace { id }
        workItemType { name }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_MEMBERS_QUERY = """
query WorklogHubProjectMembers($fullPath: ID!, $after: String) {
  project(fullPath: $fullPath) {
    projectMembers(first: 100, after: $after) {
      nodes {
        id expiresAt
        accessLevel { stringValue integerValue }
        user { id username name webUrl }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_ISSUES_QUERY = """
query WorklogHubProjectIssues($fullPath: ID!, $after: String) {
  project(fullPath: $fullPath) {
    issues(first: 100, after: $after) {
      nodes {
        id iid title description state type createdAt updatedAt webUrl
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_MERGE_REQUESTS_QUERY = """
query WorklogHubProjectMergeRequests($fullPath: ID!, $after: String) {
  project(fullPath: $fullPath) {
    mergeRequests(first: 100, after: $after) {
      nodes {
        id iid title description state createdAt updatedAt webUrl
        sourceBranch targetBranch
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_BRANCHES_QUERY = """
query WorklogHubProjectBranches($fullPath: ID!, $offset: Int!) {
  project(fullPath: $fullPath) {
    repository {
      rootRef
      branchCount
      branchNames(searchPattern: "*", offset: $offset, limit: 100)
    }
  }
}
"""


class GitLabCatalogCollector:
    source_name = "gitlab"

    def __init__(self, client: GitLabGraphQLClient) -> None:
        self.client = client

    def collect_catalog(self) -> CatalogSnapshot:
        groups = self.collect_groups()
        projects = self.collect_projects()
        memberships: list[CatalogMembership] = []
        work_items: list[CatalogWorkItem] = []
        branches: list[CatalogBranch] = []
        iterations: list[CatalogIteration] = []

        for group in groups:
            variables = {"fullPath": group.full_path}
            memberships.extend(
                self._normalize_memberships(
                    self._connection_nodes(
                        GROUP_MEMBERS_QUERY,
                        "group",
                        "groupMembers",
                        variables,
                    ),
                    "group",
                    group.external_id,
                )
            )
            iterations.extend(
                self._normalize_iterations(
                    self._connection_nodes(
                        GROUP_ITERATIONS_QUERY,
                        "group",
                        "iterations",
                        variables,
                    ),
                    group.external_id,
                )
            )
            work_items.extend(
                self._normalize_epics(
                    self._connection_nodes(
                        GROUP_EPICS_QUERY,
                        "group",
                        "workItems",
                        variables,
                    ),
                    group.external_id,
                )
            )

        for project in projects:
            variables = {"fullPath": project.full_path}
            memberships.extend(
                self._normalize_memberships(
                    self._connection_nodes(
                        PROJECT_MEMBERS_QUERY,
                        "project",
                        "projectMembers",
                        variables,
                    ),
                    "project",
                    project.external_id,
                )
            )
            work_items.extend(
                self._normalize_project_items(
                    self._connection_nodes(
                        PROJECT_ISSUES_QUERY,
                        "project",
                        "issues",
                        variables,
                    ),
                    project.external_id,
                    is_merge_request=False,
                )
            )
            work_items.extend(
                self._normalize_project_items(
                    self._connection_nodes(
                        PROJECT_MERGE_REQUESTS_QUERY,
                        "project",
                        "mergeRequests",
                        variables,
                    ),
                    project.external_id,
                    is_merge_request=True,
                )
            )
            branches.extend(self._collect_branches(project))

        return CatalogSnapshot(
            groups=groups,
            projects=projects,
            memberships=memberships,
            work_items=work_items,
            branches=branches,
            iterations=iterations,
        )

    def collect_groups(self) -> list[CatalogGroup]:
        return [
            CatalogGroup(
                external_id=str(node["id"]),
                name=str(node["name"]),
                full_path=str(node["fullPath"]),
                web_url=_optional_string(node.get("webUrl")),
                parent_external_id=_nested_id(node.get("parent")),
                raw=node,
            )
            for node in self._connection_nodes(GROUPS_QUERY, None, "groups", {})
        ]

    def collect_projects(self) -> list[CatalogProject]:
        projects: list[CatalogProject] = []
        for node in self._connection_nodes(PROJECTS_QUERY, None, "projects", {}):
            repository = node.get("repository") or {}
            projects.append(
                CatalogProject(
                    external_id=str(node["id"]),
                    name=str(node["name"]),
                    full_path=str(node["fullPath"]),
                    web_url=_optional_string(node.get("webUrl")),
                    group_external_id=_nested_id(node.get("namespace")),
                    default_branch=_optional_string(repository.get("rootRef")),
                    archived=bool(node.get("archived", False)),
                    raw=node,
                )
            )
        return projects

    def _connection_nodes(
        self,
        query: str,
        scope_key: str | None,
        connection_key: str,
        variables: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        nodes: list[dict[str, Any]] = []
        while True:
            page_variables = {**variables, "after": cursor}
            data = self.client.execute(query, page_variables)
            scope = data if scope_key is None else data.get(scope_key)
            if not isinstance(scope, dict):
                raise GitLabError(f"В ответе отсутствует {scope_key}")
            connection = scope.get(connection_key)
            if not isinstance(connection, dict):
                raise GitLabError(f"В ответе отсутствует {connection_key}")
            page_nodes = connection.get("nodes")
            if not isinstance(page_nodes, list):
                raise GitLabError(f"В ответе отсутствует {connection_key}.nodes")
            nodes.extend(node for node in page_nodes if isinstance(node, dict))
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return nodes
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise GitLabError(
                    f"GitLab вернул некорректный курсор для {connection_key}"
                )
            seen_cursors.add(cursor)

    def _collect_branches(self, project: CatalogProject) -> list[CatalogBranch]:
        result: list[CatalogBranch] = []
        offset = 0
        while True:
            data = self.client.execute(
                PROJECT_BRANCHES_QUERY,
                {"fullPath": project.full_path, "offset": offset},
            )
            project_data = data.get("project")
            if not isinstance(project_data, dict):
                raise GitLabError("В ответе отсутствует project")
            repository = project_data.get("repository")
            if repository is None:
                return result
            if not isinstance(repository, dict):
                raise GitLabError("В ответе отсутствует project.repository")
            names = repository.get("branchNames") or []
            if not isinstance(names, list):
                raise GitLabError("Некорректный список branchNames")
            root_ref = _optional_string(repository.get("rootRef"))
            result.extend(
                CatalogBranch(
                    project_external_id=project.external_id,
                    name=str(name),
                    is_default=str(name) == root_ref,
                )
                for name in names
            )
            offset += len(names)
            total = int(repository.get("branchCount") or 0)
            if not names or offset >= total:
                return result

    @staticmethod
    def _normalize_memberships(
        nodes: list[dict[str, Any]],
        scope_kind: str,
        scope_external_id: str,
    ) -> list[CatalogMembership]:
        result: list[CatalogMembership] = []
        for node in nodes:
            user = node.get("user")
            if not isinstance(user, dict):
                continue
            access = node.get("accessLevel") or {}
            result.append(
                CatalogMembership(
                    external_id=str(node["id"]),
                    scope_kind=scope_kind,
                    scope_external_id=scope_external_id,
                    user=CatalogUser(
                        external_id=str(user["id"]),
                        username=str(user["username"]),
                        display_name=_optional_string(user.get("name")),
                        web_url=_optional_string(user.get("webUrl")),
                        raw=user,
                    ),
                    access_level=_optional_string(access.get("stringValue")),
                    access_level_value=_optional_int(access.get("integerValue")),
                    expires_at=_optional_string(node.get("expiresAt")),
                    raw=node,
                )
            )
        return result

    @staticmethod
    def _normalize_iterations(
        nodes: list[dict[str, Any]], group_external_id: str
    ) -> list[CatalogIteration]:
        return [
            CatalogIteration(
                external_id=str(node["id"]),
                group_external_id=_nested_id(node.get("group")) or group_external_id,
                iid=str(node["iid"]),
                title=_optional_string(node.get("title")),
                description=_optional_string(node.get("description")),
                state=str(node["state"]),
                start_date=_optional_string(node.get("startDate")),
                due_date=_optional_string(node.get("dueDate")),
                web_url=_optional_string(node.get("webUrl")),
                raw=node,
            )
            for node in nodes
        ]

    @staticmethod
    def _normalize_epics(
        nodes: list[dict[str, Any]], group_external_id: str
    ) -> list[CatalogWorkItem]:
        result: list[CatalogWorkItem] = []
        for node in nodes:
            type_data = node.get("workItemType") or {}
            kind = str(type_data.get("name") or "epic").lower()
            result.append(
                CatalogWorkItem(
                    external_id=str(node["id"]),
                    kind=kind,
                    iid=str(node["iid"]),
                    title=str(node["title"]),
                    description=_optional_string(node.get("description")),
                    state=_optional_string(node.get("state")),
                    web_url=_optional_string(node.get("webUrl")),
                    project_external_id=None,
                    group_external_id=_nested_id(node.get("namespace"))
                    or group_external_id,
                    branch_name=None,
                    created_at=_optional_string(node.get("createdAt")),
                    updated_at=_optional_string(node.get("updatedAt")),
                    raw=node,
                )
            )
        return result

    @staticmethod
    def _normalize_project_items(
        nodes: list[dict[str, Any]],
        project_external_id: str,
        *,
        is_merge_request: bool,
    ) -> list[CatalogWorkItem]:
        result: list[CatalogWorkItem] = []
        for node in nodes:
            kind = (
                "merge_request"
                if is_merge_request
                else str(node.get("type") or "issue").lower()
            )
            result.append(
                CatalogWorkItem(
                    external_id=str(node["id"]),
                    kind=kind,
                    iid=str(node["iid"]),
                    title=str(node["title"]),
                    description=_optional_string(node.get("description")),
                    state=_optional_string(node.get("state")),
                    web_url=_optional_string(node.get("webUrl")),
                    project_external_id=project_external_id,
                    group_external_id=None,
                    branch_name=_optional_string(node.get("sourceBranch")),
                    created_at=_optional_string(node.get("createdAt")),
                    updated_at=_optional_string(node.get("updatedAt")),
                    raw=node,
                )
            )
        return result


def _nested_id(value: Any) -> str | None:
    return str(value["id"]) if isinstance(value, dict) and value.get("id") else None


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
