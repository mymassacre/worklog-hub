from __future__ import annotations

import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.helpers import gitlab_timelog
from tests.test_sync import FakeCollector
from worklog_hub.adapters.gitlab import normalize_timelog
from worklog_hub.adapters.jira import (
    API,
    JiraCollector,
    JiraError,
    JiraRESTClient,
    _NoRedirect,
    normalize_worklog,
)
from worklog_hub.config import JiraConfig, ProjectScope
from worklog_hub.database import Database
from worklog_hub.sync import synchronize

PROJECT = {"id": "100", "key": "ABC", "name": "Тестовый проект"}
ISSUE = {
    "id": "101",
    "key": "ABC-1",
    "fields": {
        "project": PROJECT,
        "summary": "Тестовая задача",
        "issuetype": {"name": "Task", "subtask": False},
        "status": {"name": "Open"},
    },
}
WORKLOG = {
    "id": "500",
    "issueId": "101",
    "author": {"key": "user-1", "name": "tester", "displayName": "Тест"},
    "started": "2026-10-10T00:30:00.000+0700",
    "timeSpentSeconds": 3600,
    "created": "2026-11-02T12:00:00.000+0700",
    "comment": "Тестовая работа",
}


class Fixture:
    def __init__(self) -> None:
        self.calls = []
        self.issues = [deepcopy(ISSUE)]
        self.worklogs = [deepcopy(WORKLOG)]

    def __call__(self, resource: str, params: dict) -> object:
        self.calls.append((resource, params))
        if resource == API + "/myself":
            return {"key": "user-1", "name": "tester"}
        if resource == API + "/project":
            return [deepcopy(PROJECT)]
        if resource == API + "/project/ABC":
            return deepcopy(PROJECT)
        if resource == API + "/search":
            start = params["startAt"]
            return {
                "startAt": start,
                "total": len(self.issues),
                "maxResults": 1,
                "issues": deepcopy(self.issues[start : start + 1]),
            }
        if resource.endswith("/worklog"):
            issue_id = resource.split("/")[-2]
            logs = [w for w in self.worklogs if w["issueId"] == issue_id]
            return {
                "startAt": 0,
                "total": len(logs),
                "maxResults": len(logs),
                "worklogs": deepcopy(logs),
            }
        raise AssertionError(f"Неожиданный маршрут: {resource}")


def collector(fixture: Fixture) -> JiraCollector:
    return JiraCollector(
        JiraRESTClient(
            JiraConfig("https://jira.example/jira", "fake-pat", ("ABC",)), fixture
        )
    )


class JiraTests(unittest.TestCase):
    def test_rejects_malformed_worklog_fields(self) -> None:
        for change in (
            {"started": "2026-10-10T12:00:00"},
            {"timeSpentSeconds": True},
            {"issueId": "999"},
            {"author": {}},
            {"comment": {"type": "doc"}},
        ):
            raw = {**deepcopy(WORKLOG), **change}
            with self.subTest(change=change), self.assertRaises(JiraError):
                normalize_worklog(raw, ISSUE, PROJECT, "https://example")

    def test_source_url_cannot_mix_servers_with_same_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "test.sqlite3")
            database.migrate()
            database.ensure_source("jira", "https://jira-one.example")
            with self.assertRaisesRegex(ValueError, "другой сервер"):
                database.ensure_source("jira", "https://jira-two.example")
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT base_url FROM sources").fetchone()[0],
                    "https://jira-one.example",
                )

    def test_normalizes_actual_work_date_and_user_identity(self) -> None:
        entry = normalize_worklog(WORKLOG, ISSUE, PROJECT, "https://jira.example")
        self.assertEqual(entry.spent_at.isoformat(), "2026-10-09T17:30:00+00:00")
        self.assertEqual(entry.spent_date, date(2026, 10, 10))
        self.assertEqual(entry.seconds, 3600)
        self.assertEqual(entry.user.external_id, "user-1")
        self.assertEqual(entry.work_item.iid, "ABC-1")
        self.assertEqual(entry.project.full_path, "ABC")

    def test_paginates_issues_and_filters_started_not_created(self) -> None:
        fixture = Fixture()
        issue2 = deepcopy(ISSUE)
        issue2.update(id="102", key="ABC-2")
        fixture.issues.append(issue2)
        outside = deepcopy(WORKLOG)
        outside.update(id="501", issueId="102", started="2026-09-30T12:00:00+0700")
        fixture.worklogs.append(outside)
        entry_list = collector(fixture).collect(date(2026, 10, 10), date(2026, 10, 10))
        self.assertEqual([entry.external_id for entry in entry_list], ["500"])
        pages = [p for r, p in fixture.calls if r == API + "/search"]
        self.assertEqual([p["startAt"] for p in pages], [0, 1])
        self.assertIn('project IN ("ABC")', pages[0]["jql"])
        self.assertNotIn("worklogDate", pages[0]["jql"])
        self.assertEqual(
            [p for r, p in fixture.calls if r.endswith("/worklog")], [{}, {}]
        )

    def test_catalog_and_entries_share_search_and_collect_epics(self) -> None:
        fixture = Fixture()
        fixture.issues[0]["fields"]["issuetype"]["name"] = "Epic"
        adapter = collector(fixture)
        catalog = adapter.collect_catalog()
        adapter.collect(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(catalog.projects[0].full_path, "ABC")
        self.assertEqual(catalog.work_items[0].kind, "epic")
        self.assertEqual(len([r for r, _ in fixture.calls if r == API + "/search"]), 1)

    def test_rejects_incomplete_worklog_and_invalid_pagination(self) -> None:
        for malformed in (
            {"startAt": 0, "total": 2, "worklogs": [WORKLOG]},
            {"startAt": 0, "worklogs": []},
            {"startAt": 1, "total": 1, "worklogs": [WORKLOG]},
        ):
            fixture = Fixture()

            def transport(resource, params, malformed=malformed, fixture=fixture):
                return (
                    malformed
                    if resource.endswith("/worklog")
                    else fixture(resource, params)
                )

            with self.subTest(response=malformed), self.assertRaises(JiraError):
                JiraCollector(
                    JiraRESTClient(
                        JiraConfig("https://example", "fake", ("ABC",)), transport
                    )
                ).collect(date(2026, 10, 1), date(2026, 10, 31))

    def test_rejects_other_project_and_ignored_offset(self) -> None:
        for mode in ("other-project", "repeated-page"):
            fixture = Fixture()
            if mode == "other-project":
                fixture.issues[0]["fields"]["project"] = {"id": "999"}
            else:
                fixture.issues.append(deepcopy(ISSUE))
            with self.subTest(mode=mode), self.assertRaises(JiraError):
                collector(fixture).collect(date(2026, 10, 1), date(2026, 10, 31))

    def test_http_is_get_with_bearer_and_preserves_context_path(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json;charset=UTF-8"}
        response.read.return_value = b'{"name":"tester"}'
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            user = JiraRESTClient(JiraConfig("https://example/jira", "fake-pat")).get(
                API + "/myself"
            )
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example/jira/rest/api/2/myself")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-pat")
        self.assertEqual(user["name"], "tester")

    def test_html_http_errors_and_redirects_are_not_success(self) -> None:
        with self.assertRaisesRegex(JiraError, "редирект"):
            _NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://sso.example"
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "text/html"}
        with patch("urllib.request.build_opener") as build:
            client = JiraRESTClient(JiraConfig("https://example", "fake-pat"))
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(JiraError, "SSO"):
                client.get(API + "/myself")
            build.return_value.open.side_effect = urllib.error.HTTPError(
                "https://example", 403, "", {}, None
            )
            with self.assertRaisesRegex(JiraError, "403"):
                client.get(API + "/myself")

    def test_transport_error_is_redacted(self) -> None:
        def transport(resource, params):
            raise OSError("failed fake-pat")

        with self.assertRaises(JiraError) as caught:
            JiraRESTClient(JiraConfig("https://example", "fake-pat"), transport).get(
                API + "/myself"
            )
        self.assertNotIn("fake-pat", str(caught.exception))

    def test_sync_updates_but_preserves_hidden_entries_and_other_sources(self) -> None:
        fixture = Fixture()
        period = (date(2026, 10, 1), date(2026, 10, 31))
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "test.sqlite3")
            synchronize(
                database, FakeCollector([normalize_timelog(gitlab_timelog())]), *period
            )

            def run():
                adapter = collector(fixture)
                return synchronize(
                    database,
                    adapter,
                    *period,
                    catalog_collector=adapter,
                    scope=ProjectScope(("ABC",)),
                )

            run()
            fixture.worklogs[0]["timeSpentSeconds"] = 7200
            run()
            fixture.worklogs.clear()
            self.assertEqual(run().deleted, 0)
            with closing(sqlite3.connect(database.path)) as connection:
                rows = connection.execute(
                    "SELECT s.name, t.seconds FROM time_entries t "
                    "JOIN sources s ON s.id=t.source_id ORDER BY s.name"
                ).fetchall()
                self.assertEqual(rows, [("gitlab", 3600), ("jira", 7200)])
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM entity_links e "
                        "JOIN work_items w ON w.id=e.left_work_item_id "
                        "JOIN sources s ON s.id=w.source_id WHERE s.name='jira'"
                    ).fetchone()[0],
                    0,
                )

    def test_failed_worklog_does_not_persist_partial_catalog(self) -> None:
        fixture = Fixture()

        def transport(resource, params):
            if resource.endswith("/worklog"):
                raise JiraError("403")
            return fixture(resource, params)

        adapter = JiraCollector(
            JiraRESTClient(JiraConfig("https://example", "fake", ("ABC",)), transport)
        )
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "test.sqlite3")
            with self.assertRaises(JiraError):
                synchronize(
                    database,
                    adapter,
                    date(2026, 10, 1),
                    date(2026, 10, 31),
                    catalog_collector=adapter,
                )
            with closing(sqlite3.connect(database.path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
                )
                self.assertEqual(
                    connection.execute("SELECT status FROM sync_runs").fetchone()[0],
                    "failed",
                )
