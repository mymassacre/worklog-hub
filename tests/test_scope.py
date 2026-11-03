from __future__ import annotations

import unittest
from datetime import date

from tests.helpers import gitlab_timelog
from tests.test_gitlab import _connection
from worklog_hub.adapters.gitlab import (
    GitLabCollector,
    GitLabError,
    GitLabGraphQLClient,
)
from worklog_hub.adapters.gitlab_catalog import GitLabCatalogCollector
from worklog_hub.config import GitLabConfig, GroupScope


def group(path: str) -> dict:
    return {"id": path, "name": path, "fullPath": path, "parent": None}


def project(path: str) -> dict:
    return {
        "id": path,
        "name": path,
        "fullPath": path,
        "namespace": {"id": path.rsplit("/", 1)[0]},
    }


class ScopeTests(unittest.TestCase):
    def test_available_groups_paginates_outside_config_and_preserves_scope(
        self,
    ) -> None:
        variables_seen = []

        def transport(query: str, variables: dict) -> dict:
            self.assertIn("allAvailable: true", query)
            variables_seen.append(variables)
            if variables["after"] is None:
                connection = _connection([group("z-group")])
                connection["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
            else:
                connection = _connection([group("a-group"), group("z-group")])
            return {"data": {"groups": connection}}

        config = GitLabConfig(
            "https://example", "secret", scope=GroupScope(("selected",))
        )
        catalog = GitLabCatalogCollector(GitLabGraphQLClient(config, transport))
        groups = catalog.list_available_groups("group")
        self.assertEqual([g.full_path for g in groups], ["a-group", "z-group"])
        self.assertEqual(
            variables_seen,
            [{"search": "group", "after": None}, {"search": "group", "after": "next"}],
        )
        self.assertEqual(config.scope.paths, ("selected",))

    def test_overlapping_roots_and_subgroups_collect_projects_once(self) -> None:
        calls: list[tuple[str, dict]] = []

        def transport(query: str, variables: dict) -> dict:
            calls.append((query, variables))
            path = variables.get("fullPath")
            if "WorklogHubGroup(" in query:
                return {"data": {"group": group(path)}}
            if "WorklogHubDescendantGroups" in query:
                nodes = [group("team/sub")] if path == "team" else []
                return {"data": {"group": {"descendantGroups": _connection(nodes)}}}
            if "WorklogHubGroupProjects" in query:
                nodes = [project(f"{path}/app"), project("team-other/shared")]
                return {"data": {"group": {"projects": _connection(nodes)}}}
            if "WorklogHubTimelogs" in query:
                project_path = variables["projectId"]
                raw = gitlab_timelog(project_path)
                raw["project"]["id"] = project_path
                raw["project"]["fullPath"] = project_path
                return {"data": {"timelogs": _connection([raw, raw])}}
            self.fail(f"Неожиданный запрос: {query}")

        client = GitLabGraphQLClient(
            GitLabConfig(
                "https://example", "secret", scope=GroupScope(("team", "team/sub"))
            ),
            transport,
        )
        catalog = GitLabCatalogCollector(client)
        self.assertEqual(
            {g.full_path for g in catalog.collect_groups()}, {"team", "team/sub"}
        )
        entries = GitLabCollector(client, catalog).collect(
            date(2026, 10, 1), date(2026, 10, 31)
        )
        self.assertEqual({e.external_id for e in entries}, {"team/app", "team/sub/app"})
        queries = [v["projectId"] for q, v in calls if "WorklogHubTimelogs" in q]
        self.assertEqual(queries, ["team/app", "team/sub/app"])
        self.assertEqual(
            len([q for q, _ in calls if "WorklogHubGroupProjects" in q]), 2
        )
        self.assertFalse(
            any("allAvailable" in q or "membership: true" in q for q, _ in calls)
        )

    def test_project_pagination_and_include_subgroups_false(self) -> None:
        cursors = []

        def transport(query: str, variables: dict) -> dict:
            if "WorklogHubGroup(" in query:
                return {"data": {"group": group("team")}}
            self.assertIn("WorklogHubGroupProjects", query)
            cursors.append(variables["after"])
            if variables["after"] is None:
                connection = _connection([project("team/app1")])
                connection["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
            else:
                connection = _connection([project("team/app2")])
            return {"data": {"group": {"projects": connection}}}

        client = GitLabGraphQLClient(
            GitLabConfig(
                "https://example", "secret", scope=GroupScope(("team",), False)
            ),
            transport,
        )
        projects = GitLabCatalogCollector(client).collect_projects()
        self.assertEqual([p.full_path for p in projects], ["team/app1", "team/app2"])
        self.assertEqual(cursors, [None, "next"])

    def test_missing_group_stops_before_timelogs(self) -> None:
        def transport(query: str, variables: dict) -> dict:
            self.assertIn("WorklogHubGroup(", query)
            return {"data": {"group": None}}

        client = GitLabGraphQLClient(
            GitLabConfig("https://example", "secret", scope=GroupScope(("missing",))),
            transport,
        )
        with self.assertRaisesRegex(GitLabError, "не найдена или недоступна"):
            GitLabCollector(client).collect(date(2026, 10, 1), date(2026, 10, 31))

    def test_empty_scope_does_not_query_gitlab(self) -> None:
        def transport(query: str, variables: dict) -> dict:
            self.fail("Пустая область не должна вызывать API")

        with self.assertRaisesRegex(GitLabError, "Не заданы группы"):
            GitLabCatalogCollector(
                GitLabGraphQLClient(
                    GitLabConfig("https://example", "secret"), transport
                )
            ).collect_catalog()

    def test_repeated_cursor_fails_instead_of_accepting_partial_projects(self) -> None:
        def transport(query: str, variables: dict) -> dict:
            if "WorklogHubGroup(" in query:
                return {"data": {"group": group("team")}}
            return {
                "data": {
                    "group": {
                        "projects": {
                            "nodes": [project("team/app")],
                            "pageInfo": {"hasNextPage": True, "endCursor": "same"},
                        }
                    }
                }
            }

        client = GitLabGraphQLClient(
            GitLabConfig(
                "https://example", "secret", scope=GroupScope(("team",), False)
            ),
            transport,
        )
        with self.assertRaisesRegex(GitLabError, "курсор"):
            GitLabCatalogCollector(client).collect_projects()
