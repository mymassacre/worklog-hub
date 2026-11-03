from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from tests.helpers import gitlab_timelog
from tests.test_sync import FakeCollector
from worklog_hub.adapters.gitlab import normalize_timelog
from worklog_hub.cli import main
from worklog_hub.database import JIRA_KEY_RE, Database
from worklog_hub.domain import Person, Project, WorkItem
from worklog_hub.mapping import MappingError, MappingService
from worklog_hub.sync import synchronize


class FakeJira(FakeCollector):
    source_name = "jira"
    reconcile_missing = False


class MappingTests(unittest.TestCase):
    def test_keys_support_short_and_underscore_project_keys(self) -> None:
        self.assertEqual(
            JIRA_KEY_RE.findall("A-1 A_B-2 ABC-123"), ["A-1", "A_B-2", "ABC-123"]
        )

    def test_upgrades_legacy_schema_without_changing_existing_entries(self) -> None:
        legacy = Database(Path(self.tempdir.name) / "legacy.sqlite3")
        with legacy.connect() as connection, connection:
            connection.execute(
                "CREATE TABLE schema_migrations("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in sorted(
                files("worklog_hub.migrations").iterdir(), key=lambda f: f.name
            ):
                if migration.name.endswith(".sql") and migration.name[:3] < "004":
                    connection.executescript(migration.read_text(encoding="utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations VALUES (?, 'now')",
                        (migration.name[:3],),
                    )
            connection.executescript("""
                INSERT INTO sources(id,name,created_at,updated_at)
                VALUES (1,'gitlab','now','now');
                INSERT INTO users(id,source_id,external_id,username,loaded_at)
                VALUES (1,1,'old-user','old','now');
                INSERT INTO projects(id,source_id,external_id,name,loaded_at)
                VALUES (1,1,'old-project','Old','now');
                INSERT INTO sync_runs(id,source_id,started_at,date_from,date_to,status)
                VALUES (1,1,'now','2026-10-01','2026-10-31','success');
                INSERT INTO time_entries(
                    source_id,external_id,user_id,project_id,spent_at,
                    spent_date,seconds,loaded_at,raw_json,last_seen_run_id)
                VALUES (1,'old-entry',1,1,'2026-10-10T12:00:00Z',
                        '2026-10-10',3600,'now','{}',1);
            """)
            before = tuple(connection.execute("SELECT * FROM time_entries").fetchone())
        legacy.migrate()
        legacy.migrate()
        with legacy.connect() as connection:
            self.assertEqual(
                tuple(connection.execute("SELECT * FROM time_entries").fetchone()),
                before,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM entity_mappings").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
                    0
                ],
                4,
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "test.sqlite3"
        self.database = Database(self.path)
        self.period = (date(2026, 10, 1), date(2026, 10, 31))
        first = normalize_timelog(gitlab_timelog())
        self.gitlab = replace(
            first, project=replace(first.project, full_path="team/repo")
        )
        self.other = replace(
            self.gitlab,
            external_id="gl-time-2",
            project=Project("gl-project-other", "Other", "team/other"),
            work_item=WorkItem(
                "gl-issue-other", "merge_request", "ABC-123 Сделать MR", "7"
            ),
        )
        self.jira = replace(
            first,
            external_id="jira-time-1",
            project=Project("jira-project-1", "Jira ABC", "ABC"),
            work_item=WorkItem("jira-issue-1", "issue", "Сделать отчёт", "ABC-123"),
            user=Person("jira-user-1", "ivan", "Иван"),
        )
        self.jira_other = replace(
            self.jira,
            external_id="jira-time-2",
            project=Project("jira-project-2", "Jira XYZ", "XYZ"),
            work_item=WorkItem("jira-issue-2", "issue", "Сделать другое", "XYZ-2"),
            user=Person("jira-user-2", "petrov", "Петров"),
        )
        synchronize(
            self.database, FakeCollector([self.gitlab, self.other]), *self.period
        )
        synchronize(self.database, FakeJira([self.jira, self.jira_other]), *self.period)
        self.service = MappingService(self.database)

    def project_pair(self) -> int:
        return self.service.link("project", "team/repo", "ABC")

    def task_candidate(self) -> dict:
        self.project_pair()
        self.service.suggest("task")
        rows = self.service.list("task", "proposed")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def update_title(self, title: str) -> None:
        with self.database.connect() as connection, connection:
            connection.execute(
                "UPDATE work_items SET title=? WHERE external_id=?",
                (title, self.gitlab.work_item.external_id),
            )

    def test_manual_projects_are_many_to_many_and_idempotent(self) -> None:
        first_id = self.project_pair()
        self.assertEqual(self.project_pair(), first_id)
        self.service.link("project", "team/repo", "XYZ")
        self.service.link("project", "team/other", "ABC")
        rows = self.service.list("project", "confirmed")
        self.assertEqual(len(rows), 3)

    def test_task_suggestions_require_project_scope_and_real_jira_issue(self) -> None:
        self.assertEqual(self.service.suggest("task"), 0)
        row = self.task_candidate()
        self.assertEqual((row["gitlab"], row["jira"]), ("team/repo#12", "ABC-123"))
        self.assertEqual(row["status"], "proposed")
        self.assertEqual(row["evidence"], "title: ABC-123")
        self.assertEqual(self.service.suggest("task"), 0)

    def test_manual_task_links_support_several_gitlab_items_for_one_jira(self) -> None:
        self.service.link("task", "team/repo#12", "ABC-123")
        self.service.link("task", "team/other!7", "ABC-123")
        self.assertEqual(len(self.service.list("task", "confirmed")), 2)

    def test_description_and_branch_are_evidence_not_duplicate_links(self) -> None:
        with self.database.connect() as connection, connection:
            connection.execute(
                "UPDATE work_items SET branch_name=?, raw_json=? WHERE external_id=?",
                (
                    "ABC-123-branch",
                    json.dumps({"description": "См. ABC-123"}),
                    self.gitlab.work_item.external_id,
                ),
            )
        row = self.task_candidate()
        self.assertIn("branch: ABC-123", row["evidence"])
        self.assertIn("description: ABC-123", row["evidence"])
        self.assertEqual(len(self.service.list("task")), 1)

    def test_rejections_and_confirmations_survive_suggest_and_sync(self) -> None:
        row = self.task_candidate()
        self.service.review(row["id"], "rejected")
        self.service.suggest("task")
        self.assertEqual(self.service.list("task")[0]["status"], "rejected")
        self.service.link("task", "team/repo#12", "ABC-123")
        synchronize(
            self.database, FakeCollector([self.gitlab, self.other]), *self.period
        )
        self.service.suggest("task")
        self.assertEqual(self.service.list("task")[0]["status"], "confirmed")
        with self.database.connect() as connection:
            states = [
                r[0]
                for r in connection.execute(
                    "SELECT new_status FROM mapping_reviews "
                    "WHERE mapping_id=? ORDER BY id",
                    (row["id"],),
                )
            ]
        self.assertEqual(states, ["proposed", "rejected", "confirmed"])

    def test_removed_evidence_cannot_be_confirmed_even_before_resuggest(self) -> None:
        row = self.task_candidate()
        self.update_title("Изменённое название без ключа")
        self.assertEqual(self.service.list("task")[0]["is_current"], 0)
        with self.assertRaises(MappingError):
            self.service.review(row["id"], "confirmed")
        self.service.suggest("task")
        with self.assertRaises(MappingError):
            self.service.review(row["id"], "confirmed")
        self.update_title("Снова ABC-123")
        self.service.suggest("task")
        self.service.review(row["id"], "confirmed")

    def test_rejected_project_scope_prevents_confirmation_of_task_candidate(
        self,
    ) -> None:
        row = self.task_candidate()
        project = self.service.list("project")[0]
        self.service.review(project["id"], "rejected")
        with self.assertRaisesRegex(MappingError, "проектов"):
            self.service.review(row["id"], "confirmed")

    def test_user_suggestion_is_not_automatic_confirmation(self) -> None:
        self.service.suggest("user")
        rows = self.service.list("user")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "proposed")
        self.assertEqual(rows[0]["method"], "username")
        self.service.review(rows[0]["id"], "confirmed")

    def test_user_mapping_conflicts_are_atomic_and_can_be_replaced_explicitly(
        self,
    ) -> None:
        first_id = self.service.link("user", "ivan", "petrov")
        self.service.suggest("user")
        candidate = self.service.list("user", "proposed")[0]
        with self.assertRaises(MappingError):
            self.service.review(candidate["id"], "confirmed")
        self.assertEqual(len(self.service.list("user", "confirmed")), 1)
        self.assertEqual(self.service.list("user", "proposed")[0]["status"], "proposed")
        self.service.review(first_id, "rejected")
        self.service.review(candidate["id"], "confirmed")

    def test_missing_ambiguous_and_inactive_entities_are_not_guessed(self) -> None:
        with self.assertRaises(MappingError):
            self.service.link("user", "missing", "ivan")
        with self.database.connect() as connection, connection:
            source = connection.execute(
                "SELECT id FROM sources WHERE name='gitlab'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO users(source_id,external_id,username,loaded_at) "
                "VALUES (?,?,?,?)",
                (source, "other-user", "ivan", "2026-10-01"),
            )
        with self.assertRaises(MappingError):
            self.service.link("user", "ivan", "ivan")
        self.service.link("user", self.gitlab.user.external_id, "ivan")
        with self.database.connect() as connection, connection:
            connection.execute(
                "UPDATE projects SET is_active=0 WHERE full_path='team/repo'"
            )
        with self.assertRaises(MappingError):
            self.project_pair()

    def test_mapping_never_modifies_time_entries(self) -> None:
        with self.database.connect() as connection:
            before = [
                tuple(row)
                for row in connection.execute("SELECT * FROM time_entries ORDER BY id")
            ]
        self.project_pair()
        self.service.suggest()
        for row in self.service.list(status="proposed"):
            self.service.review(row["id"], "confirmed")
        with self.database.connect() as connection:
            after = [
                tuple(row)
                for row in connection.execute("SELECT * FROM time_entries ORDER BY id")
            ]
        self.assertEqual(before, after)

    def test_migration_preserves_existing_data_and_fk_constraints(self) -> None:
        self.database.migrate()
        self.database.migrate()
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM time_entries").fetchone()[0], 4
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO entity_mappings("
                    "kind,status,method,evidence,created_at,updated_at) "
                    "VALUES ('user','confirmed','manual','test','now','now')"
                )

    def test_cli_mapping_is_offline_and_accepts_database_only_config(self) -> None:
        config_path = Path(self.tempdir.name) / "app.toml"
        config_path.write_text('[database]\npath = "test.sqlite3"\n', encoding="utf-8")
        output = io.StringIO()
        with (
            patch("worklog_hub.cli.load_token") as load,
            contextlib.redirect_stdout(output),
        ):
            prefix = ["--config", str(config_path), "mapping"]
            self.assertEqual(
                main([*prefix, "entities", "project", "--source", "gitlab"]), 0
            )
            self.assertEqual(
                main(
                    [
                        *prefix,
                        "link",
                        "project",
                        "--gitlab",
                        "team/repo",
                        "--jira",
                        "ABC",
                    ]
                ),
                0,
            )
            self.assertEqual(main([*prefix, "suggest", "--kind", "task"]), 0)
            row = self.service.list("task", "proposed")[0]
            self.assertEqual(main([*prefix, "confirm", str(row["id"])]), 0)
            self.assertEqual(main([*prefix, "list", "--status", "confirmed"]), 0)
            self.assertEqual(main([*prefix, "reject", str(row["id"])]), 0)
        load.assert_not_called()
        self.assertIn("team/repo", output.getvalue())
        self.assertIn("ABC-123", output.getvalue())
