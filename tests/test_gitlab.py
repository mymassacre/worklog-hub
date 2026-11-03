from __future__ import annotations

import unittest
from datetime import date

from tests.helpers import gitlab_timelog
from worklog_hub.adapters.gitlab import (
    GitLabCollector,
    GitLabError,
    GitLabGraphQLClient,
    normalize_timelog,
)
from worklog_hub.adapters.gitlab_catalog import GitLabCatalogCollector
from worklog_hub.config import GitLabConfig, GroupScope


def _connection(nodes: list[dict]) -> dict:
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }


def _config() -> GitLabConfig:
    return GitLabConfig(
        "https://gitlab.example", "secret", scope=GroupScope(("group",), False)
    )


def _scope_response(query: str) -> dict | None:
    if "WorklogHubGroup(" in query:
        return {
            "data": {
                "group": {
                    "id": "group-1",
                    "name": "Group",
                    "fullPath": "group",
                    "webUrl": None,
                    "parent": None,
                }
            }
        }
    if "WorklogHubGroupProjects" in query:
        return {
            "data": {
                "group": {
                    "projects": _connection(
                        [
                            {
                                "id": "gid://gitlab/Project/2",
                                "name": "Project",
                                "fullPath": "group/project",
                                "namespace": {"id": "group-1"},
                            }
                        ]
                    )
                }
            }
        }
    return None


class GitLabNormalizationTests(unittest.TestCase):
    def test_normalizes_timezone_and_preserves_actual_date(self) -> None:
        entry = normalize_timelog(gitlab_timelog())
        self.assertEqual(entry.spent_at.isoformat(), "2026-10-10T10:30:00+00:00")
        self.assertEqual(entry.spent_date.isoformat(), "2026-10-10")
        self.assertEqual(entry.seconds, 3600)
        if entry.work_item is None:
            self.fail("У тестового timelog должен быть связанный work item")
        self.assertEqual(entry.work_item.kind, "issue")

    def test_paginates_until_has_next_page_is_false(self) -> None:
        cursors: list[str | None] = []

        def transport(query: str, variables: dict) -> dict:
            if (response := _scope_response(query)) is not None:
                return response
            cursors.append(variables["after"])
            if variables["after"] is None:
                return {
                    "data": {
                        "timelogs": {
                            "nodes": [gitlab_timelog("one")],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            return {
                "data": {
                    "timelogs": {
                        "nodes": [gitlab_timelog("two")],
                        "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                    }
                }
            }

        config = _config()
        collector = GitLabCollector(GitLabGraphQLClient(config, transport))
        entries = collector.collect(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual([entry.external_id for entry in entries], ["one", "two"])
        self.assertEqual(cursors, [None, "cursor-1"])

    def test_filters_by_inclusive_spent_dates(self) -> None:
        variables_seen: list[dict] = []

        def transport(query: str, variables: dict) -> dict:
            if (response := _scope_response(query)) is not None:
                return response
            variables_seen.append(variables)
            return {
                "data": {
                    "timelogs": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }

        config = _config()
        GitLabCollector(GitLabGraphQLClient(config, transport)).collect(
            date(2026, 10, 1), date(2026, 10, 31)
        )
        self.assertEqual(variables_seen[0]["startDate"], "2026-10-01")
        self.assertEqual(variables_seen[0]["endDate"], "2026-10-31")
        self.assertEqual(variables_seen[0]["projectId"], "gid://gitlab/Project/2")
        self.assertIsNone(variables_seen[0]["groupId"])

    def test_collects_scoped_timelogs_and_removes_duplicates(self) -> None:
        scopes_seen: list[tuple[str | None, str | None]] = []

        def transport(query: str, variables: dict) -> dict:
            if (response := _scope_response(query)) is not None:
                return response
            scopes_seen.append((variables["groupId"], variables["projectId"]))
            return {"data": {"timelogs": _connection([gitlab_timelog("same")])}}

        collector = GitLabCollector(GitLabGraphQLClient(_config(), transport))
        entries = collector.collect(date(2026, 10, 1), date(2026, 10, 31))

        self.assertEqual([entry.external_id for entry in entries], ["same"])
        self.assertEqual(
            scopes_seen,
            [(None, "gid://gitlab/Project/2")],
        )

    def test_client_redacts_token_from_transport_error(self) -> None:
        token = "very-secret-token"

        def transport(query: str, variables: dict) -> dict:
            raise OSError(f"failed with {token}")

        client = GitLabGraphQLClient(
            GitLabConfig("https://gitlab.example", token), transport
        )
        with self.assertRaises(GitLabError) as caught:
            client.execute("query { currentUser { id } }")
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_client_reports_graphql_operation_name(self) -> None:
        def transport(query: str, variables: dict) -> dict:
            return {"errors": [{"message": "Access denied"}]}

        client = GitLabGraphQLClient(
            GitLabConfig("https://gitlab.example", "secret"), transport
        )
        with self.assertRaises(GitLabError) as caught:
            client.execute("query WorklogHubGroups { groups { nodes { id } } }")
        self.assertIn("WorklogHubGroups", str(caught.exception))

    def test_catalog_groups_are_paginated(self) -> None:
        cursors: list[str | None] = []

        def transport(query: str, variables: dict) -> dict:
            if "WorklogHubGroup(" in query:
                return {
                    "data": {
                        "group": {
                            "id": "root",
                            "name": "Root",
                            "fullPath": "group",
                            "parent": None,
                        }
                    }
                }
            cursors.append(variables["after"])
            if variables["after"] is None:
                return {
                    "data": {
                        "group": {
                            "descendantGroups": {
                                "nodes": [
                                    {
                                        "id": "group-1",
                                        "name": "One",
                                        "fullPath": "group/one",
                                        "webUrl": "https://gitlab.example/one",
                                        "parent": None,
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "next",
                                },
                            }
                        }
                    }
                }
            return {
                "data": {
                    "group": {
                        "descendantGroups": {
                            "nodes": [
                                {
                                    "id": "group-2",
                                    "name": "Two",
                                    "fullPath": "group/one/two",
                                    "webUrl": "https://gitlab.example/one/two",
                                    "parent": {"id": "group-1"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }

        config = GitLabConfig(
            "https://gitlab.example", "secret", scope=GroupScope(("group",))
        )
        collector = GitLabCatalogCollector(GitLabGraphQLClient(config, transport))
        groups = collector.collect_groups()
        self.assertEqual(
            [group.external_id for group in groups], ["root", "group-1", "group-2"]
        )
        self.assertEqual(groups[2].parent_external_id, "group-1")
        self.assertEqual(cursors, [None, "next"])

    def test_catalog_does_not_hide_network_errors(self) -> None:
        def transport(query: str, variables: dict) -> dict:
            raise OSError("network failed")

        collector = GitLabCatalogCollector(GitLabGraphQLClient(_config(), transport))
        with self.assertRaisesRegex(GitLabError, "network failed"):
            collector.collect_catalog()

    def test_catalog_collects_iterations_epics_tasks_mrs_and_branches(self) -> None:
        def connection(nodes: list[dict]) -> dict:
            return {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }

        def transport(query: str, variables: dict) -> dict:
            if "WorklogHubGroup(" in query:
                return {
                    "data": {
                        "group": {
                            "id": "group-1",
                            "name": "Group",
                            "fullPath": "group",
                            "webUrl": None,
                            "parent": None,
                        }
                    }
                }
            if "WorklogHubGroupProjects" in query:
                return {
                    "data": {
                        "group": {
                            "projects": connection(
                                [
                                    {
                                        "id": "project-1",
                                        "name": "Project",
                                        "fullPath": "group/project",
                                        "webUrl": None,
                                        "archived": False,
                                        "namespace": {"id": "group-1"},
                                        "repository": {"rootRef": "main"},
                                    }
                                ]
                            )
                        }
                    }
                }
            if "WorklogHubGroupMembers" in query:
                return {
                    "errors": [
                        {
                            "message": (
                                "The resource that you are attempting to access "
                                "does not exist or you don't have permission to "
                                "perform this action"
                            )
                        }
                    ]
                }
            if "WorklogHubGroupIterations" in query:
                return {
                    "data": {
                        "group": {
                            "iterations": connection(
                                [
                                    {
                                        "id": "iteration-1",
                                        "iid": "1",
                                        "title": "Sprint",
                                        "description": None,
                                        "state": "opened",
                                        "startDate": "2026-10-01",
                                        "dueDate": "2026-10-14",
                                        "webUrl": None,
                                        "group": {"id": "group-1"},
                                    }
                                ]
                            )
                        }
                    }
                }
            if "WorklogHubGroupEpics" in query:
                return {
                    "data": {
                        "group": {
                            "workItems": connection(
                                [
                                    {
                                        "id": "epic-1",
                                        "iid": "1",
                                        "title": "ABC-1 Epic",
                                        "description": None,
                                        "state": "OPEN",
                                        "createdAt": "2026-10-01T00:00:00Z",
                                        "updatedAt": "2026-10-02T00:00:00Z",
                                        "webUrl": None,
                                        "namespace": {"id": "group-1"},
                                        "workItemType": {"name": "Epic"},
                                    }
                                ]
                            )
                        }
                    }
                }
            if "WorklogHubProjectMembers" in query:
                return {"data": {"project": {"projectMembers": connection([])}}}
            if "WorklogHubProjectIssues" in query:
                return {
                    "data": {
                        "project": {
                            "issues": connection(
                                [
                                    {
                                        "id": "task-1",
                                        "iid": "2",
                                        "title": "Task",
                                        "description": None,
                                        "state": "opened",
                                        "type": "TASK",
                                        "createdAt": None,
                                        "updatedAt": None,
                                        "webUrl": None,
                                    }
                                ]
                            )
                        }
                    }
                }
            if "WorklogHubProjectMergeRequests" in query:
                return {
                    "data": {
                        "project": {
                            "mergeRequests": connection(
                                [
                                    {
                                        "id": "mr-1",
                                        "iid": "3",
                                        "title": "MR",
                                        "description": None,
                                        "state": "opened",
                                        "createdAt": None,
                                        "updatedAt": None,
                                        "webUrl": None,
                                        "sourceBranch": "ABC-2-feature",
                                        "targetBranch": "main",
                                    }
                                ]
                            )
                        }
                    }
                }
            if "WorklogHubProjectBranches" in query:
                return {
                    "data": {
                        "project": {
                            "repository": {
                                "rootRef": "main",
                                "branchCount": 2,
                                "branchNames": ["main", "ABC-2-feature"],
                            }
                        }
                    }
                }
            self.fail(f"Неожиданный запрос: {query}")

        config = _config()
        snapshot = GitLabCatalogCollector(
            GitLabGraphQLClient(config, transport)
        ).collect_catalog()

        self.assertEqual(snapshot.counts()["groups"], 1)
        self.assertEqual(snapshot.counts()["projects"], 1)
        self.assertEqual(snapshot.counts()["iterations"], 1)
        self.assertIn("memberships", snapshot.incomplete_tables)
        self.assertIn("users", snapshot.incomplete_tables)
        self.assertEqual(len(snapshot.warnings), 1)
        self.assertIn("WorklogHubGroupMembers", snapshot.warnings[0])
        self.assertEqual(
            {item.kind for item in snapshot.work_items},
            {"epic", "task", "merge_request"},
        )
        self.assertEqual(
            [(branch.name, branch.is_default) for branch in snapshot.branches],
            [("main", True), ("ABC-2-feature", False)],
        )


if __name__ == "__main__":
    unittest.main()
