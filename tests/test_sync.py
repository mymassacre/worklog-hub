from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from tests.helpers import gitlab_timelog
from worklog_hub.adapters.gitlab import normalize_timelog
from worklog_hub.database import Database
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
from worklog_hub.sync import synchronize


class FakeCollector:
    source_name = "gitlab"

    def __init__(self, entries: list) -> None:
        self.entries = entries

    def check(self) -> str:
        return "fake"

    def collect(self, date_from: date, date_to: date) -> list:
        return self.entries


class FakeCatalogCollector:
    source_name = "gitlab"

    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    def collect_catalog(self) -> CatalogSnapshot:
        return self.snapshot


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "worklog.sqlite3"
        self.database = Database(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_upsert_is_idempotent_and_updates_changed_entry(self) -> None:
        first = normalize_timelog(gitlab_timelog(seconds=3600))
        changed = normalize_timelog(gitlab_timelog(seconds=7200))
        period = (date(2026, 10, 1), date(2026, 10, 31))
        synchronize(self.database, FakeCollector([first]), *period)
        synchronize(self.database, FakeCollector([changed]), *period)

        with closing(sqlite3.connect(self.db_path)) as connection:
            count, seconds = connection.execute(
                "SELECT COUNT(*), MAX(seconds) FROM time_entries"
            ).fetchone()
            link = connection.execute(
                "SELECT right_external_key, method, confidence FROM entity_links"
            ).fetchone()
        self.assertEqual((count, seconds), (1, 7200))
        self.assertEqual(link, ("ABC-123", "jira_key:title", 0.9))

    def test_full_period_reconciliation_deletes_missing_entries(self) -> None:
        one = normalize_timelog(gitlab_timelog("one", spent_at="2026-10-02T12:00:00Z"))
        two = normalize_timelog(gitlab_timelog("two", spent_at="2026-10-03T12:00:00Z"))
        period = (date(2026, 10, 1), date(2026, 10, 31))
        synchronize(self.database, FakeCollector([one, two]), *period)
        result = synchronize(self.database, FakeCollector([one]), *period)

        with closing(sqlite3.connect(self.db_path)) as connection:
            ids = [
                row[0]
                for row in connection.execute("SELECT external_id FROM time_entries")
            ]
        self.assertEqual(ids, ["one"])
        self.assertEqual(result.deleted, 1)

    def test_failed_collection_does_not_reconcile_period(self) -> None:
        entry = normalize_timelog(gitlab_timelog())
        period = (date(2026, 10, 1), date(2026, 10, 31))
        synchronize(self.database, FakeCollector([entry]), *period)

        class BrokenCollector(FakeCollector):
            def collect(self, date_from: date, date_to: date) -> list:
                raise RuntimeError("network failed")

        with self.assertRaises(RuntimeError):
            synchronize(self.database, BrokenCollector([]), *period)
        with closing(sqlite3.connect(self.db_path)) as connection:
            entry_count = connection.execute(
                "SELECT COUNT(*) FROM time_entries"
            ).fetchone()[0]
            status = connection.execute(
                "SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(entry_count, 1)
        self.assertEqual(status, "failed")

    def test_catalog_is_upserted_and_missing_entities_are_deactivated(self) -> None:
        group = CatalogGroup("group-1", "Group", "group", None, None, {"id": "group-1"})
        project = CatalogProject(
            "project-1",
            "Project",
            "group/project",
            None,
            "group-1",
            "main",
            False,
            {"id": "project-1"},
        )
        user = CatalogUser("user-1", "ivan", "Иван", None, {"id": "user-1"})
        membership = CatalogMembership(
            "member-1",
            "project",
            "project-1",
            user,
            "DEVELOPER",
            30,
            None,
            {"id": "member-1"},
        )
        item = CatalogWorkItem(
            "issue-1",
            "task",
            "1",
            "ABC-123 Task",
            None,
            "opened",
            None,
            "project-1",
            None,
            None,
            None,
            None,
            {"id": "issue-1"},
        )
        iteration = CatalogIteration(
            "iteration-1",
            "group-1",
            "1",
            "Sprint",
            None,
            "opened",
            "2026-10-01",
            "2026-10-14",
            None,
            {"id": "iteration-1"},
        )
        snapshot = CatalogSnapshot(
            [group],
            [project],
            [membership],
            [item],
            [CatalogBranch("project-1", "main", True)],
            [iteration],
        )
        period = (date(2026, 10, 1), date(2026, 10, 31))
        result = synchronize(
            self.database,
            FakeCollector([]),
            *period,
            catalog_collector=FakeCatalogCollector(snapshot),
        )
        synchronize(
            self.database,
            FakeCollector([]),
            *period,
            catalog_collector=FakeCatalogCollector(
                CatalogSnapshot([], [], [], [], [], [])
            ),
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            active = connection.execute(
                "SELECT is_active FROM groups WHERE external_id='group-1'"
            ).fetchone()[0]
            task_kind = connection.execute(
                "SELECT kind FROM work_items WHERE external_id='issue-1'"
            ).fetchone()[0]
            jira_key = connection.execute(
                "SELECT right_external_key FROM entity_links"
            ).fetchone()[0]
        self.assertEqual(result.catalog_counts["groups"], 1)
        self.assertEqual(active, 0)
        self.assertEqual(task_kind, "task")
        self.assertEqual(jira_key, "ABC-123")


if __name__ == "__main__":
    unittest.main()
