from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path

from worklog_hub.domain import CatalogSnapshot, TimeEntry

JIRA_KEY_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-\d+)(?![A-Z0-9])", re.IGNORECASE
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            migrations = files("worklog_hub.migrations")
            for migration in sorted(migrations.iterdir(), key=lambda item: item.name):
                if migration.name.endswith(".sql"):
                    version = migration.name.split("_", 1)[0]
                    exists = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                    ).fetchone()
                    if not exists:
                        connection.executescript(migration.read_text(encoding="utf-8"))
                        connection.execute(
                            "INSERT INTO schema_migrations"
                            "(version, applied_at) VALUES (?, ?)",
                            (version, utc_now()),
                        )
            connection.commit()

    def ensure_source(self, name: str, base_url: str | None = None) -> int:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO sources(name, base_url, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     base_url = COALESCE(excluded.base_url, sources.base_url),
                     updated_at = excluded.updated_at""",
                (name, base_url, now, now),
            )
            source_id = int(
                connection.execute(
                    "SELECT id FROM sources WHERE name = ?", (name,)
                ).fetchone()[0]
            )
            connection.commit()
            return source_id

    def start_run(self, source_id: int, date_from: date, date_to: date) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO sync_runs(
                       source_id, started_at, date_from, date_to, status
                   )
                   VALUES (?, ?, ?, ?, 'running')""",
                (source_id, utc_now(), date_from.isoformat(), date_to.isoformat()),
            )
            connection.commit()
            run_id = cursor.lastrowid
            if run_id is None:
                raise RuntimeError("SQLite не вернул ID запуска синхронизации")
            return run_id

    def fail_run(self, run_id: int, error_message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE sync_runs SET status='failed', finished_at=?, "
                "error_message=? WHERE id=?",
                (utc_now(), error_message, run_id),
            )
            connection.commit()

    def persist_entries(
        self,
        source_id: int,
        jira_source_id: int,
        run_id: int,
        date_from: date,
        date_to: date,
        entries: list[TimeEntry],
    ) -> int:
        loaded_at = utc_now()
        with self.connect() as connection:
            try:
                for entry in entries:
                    user_id = self._upsert_user(connection, source_id, entry, loaded_at)
                    project_id = self._upsert_project(
                        connection, source_id, entry, loaded_at
                    )
                    work_item_id = self._upsert_work_item(
                        connection, source_id, project_id, entry, loaded_at
                    )
                    connection.execute(
                        """INSERT INTO time_entries(
                               source_id, external_id, user_id, project_id,
                               work_item_id, spent_at, spent_date, seconds,
                               summary, loaded_at, raw_json, last_seen_run_id
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(source_id, external_id) DO UPDATE SET
                               user_id=excluded.user_id, project_id=excluded.project_id,
                               work_item_id=excluded.work_item_id,
                               spent_at=excluded.spent_at,
                               spent_date=excluded.spent_date, seconds=excluded.seconds,
                               summary=excluded.summary, loaded_at=excluded.loaded_at,
                               raw_json=excluded.raw_json,
                               last_seen_run_id=excluded.last_seen_run_id""",
                        (
                            source_id,
                            entry.external_id,
                            user_id,
                            project_id,
                            work_item_id,
                            entry.spent_at.isoformat().replace("+00:00", "Z"),
                            entry.spent_date.isoformat(),
                            entry.seconds,
                            entry.summary,
                            loaded_at,
                            json.dumps(entry.raw, ensure_ascii=False, sort_keys=True),
                            run_id,
                        ),
                    )
                    if work_item_id is not None and entry.work_item is not None:
                        self._upsert_jira_links(
                            connection,
                            work_item_id,
                            jira_source_id,
                            entry.work_item.title,
                            entry.work_item.branch_name,
                            loaded_at,
                        )

                deleted = connection.execute(
                    """DELETE FROM time_entries
                       WHERE source_id = ?
                         AND spent_date BETWEEN ? AND ?
                         AND last_seen_run_id <> ?""",
                    (source_id, date_from.isoformat(), date_to.isoformat(), run_id),
                ).rowcount
                connection.execute(
                    """UPDATE sync_runs
                       SET status='success', finished_at=?, fetched_count=?,
                           upserted_count=?, deleted_count=?
                       WHERE id=?""",
                    (utc_now(), len(entries), len(entries), deleted, run_id),
                )
                connection.commit()
                return int(deleted)
            except Exception:
                connection.rollback()
                raise

    def persist_catalog(
        self,
        source_id: int,
        jira_source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
    ) -> dict[str, int]:
        loaded_at = utc_now()
        with self.connect() as connection:
            try:
                group_ids = self._persist_catalog_groups(
                    connection, source_id, run_id, snapshot, loaded_at
                )
                project_ids = self._persist_catalog_projects(
                    connection,
                    source_id,
                    run_id,
                    snapshot,
                    group_ids,
                    loaded_at,
                )
                self._persist_catalog_memberships(
                    connection,
                    source_id,
                    run_id,
                    snapshot,
                    group_ids,
                    project_ids,
                    loaded_at,
                )
                self._persist_catalog_work_items(
                    connection,
                    source_id,
                    jira_source_id,
                    run_id,
                    snapshot,
                    group_ids,
                    project_ids,
                    loaded_at,
                )
                self._persist_catalog_branches(
                    connection,
                    source_id,
                    run_id,
                    snapshot,
                    project_ids,
                    loaded_at,
                )
                self._persist_catalog_iterations(
                    connection,
                    source_id,
                    run_id,
                    snapshot,
                    group_ids,
                    loaded_at,
                )
                self._deactivate_missing_catalog(
                    connection,
                    source_id,
                    run_id,
                    snapshot.incomplete_tables,
                )
                counts = snapshot.counts()
                connection.execute(
                    """UPDATE sync_runs
                       SET catalog_counts_json=?, warnings_json=?
                       WHERE id=?""",
                    (
                        json.dumps(counts, sort_keys=True),
                        (
                            json.dumps(snapshot.warnings, ensure_ascii=False)
                            if snapshot.warnings
                            else None
                        ),
                        run_id,
                    ),
                )
                connection.commit()
                return counts
            except Exception:
                connection.rollback()
                raise

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """SELECT r.id, s.name AS source, r.started_at, r.finished_at,
                              r.date_from, r.date_to, r.status, r.fetched_count,
                              r.upserted_count, r.deleted_count, r.error_message,
                              r.catalog_counts_json, r.warnings_json
                       FROM sync_runs r JOIN sources s ON s.id = r.source_id
                       ORDER BY r.id DESC LIMIT ?""",
                    (limit,),
                )
            )

    @staticmethod
    def _upsert_user(
        connection: sqlite3.Connection, source_id: int, entry: TimeEntry, loaded_at: str
    ) -> int:
        user = entry.user
        connection.execute(
            """INSERT INTO users(
                   source_id, external_id, username,
                   display_name, web_url, loaded_at
               )
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, external_id) DO UPDATE SET
                 username=excluded.username, display_name=excluded.display_name,
                 web_url=excluded.web_url, loaded_at=excluded.loaded_at""",
            (
                source_id,
                user.external_id,
                user.username,
                user.display_name,
                user.web_url,
                loaded_at,
            ),
        )
        return int(
            connection.execute(
                "SELECT id FROM users WHERE source_id=? AND external_id=?",
                (source_id, user.external_id),
            ).fetchone()[0]
        )

    @staticmethod
    def _upsert_project(
        connection: sqlite3.Connection, source_id: int, entry: TimeEntry, loaded_at: str
    ) -> int:
        project = entry.project
        connection.execute(
            """INSERT INTO projects(
                   source_id, external_id, name, full_path, web_url, loaded_at
               )
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, external_id) DO UPDATE SET
                 name=excluded.name, full_path=excluded.full_path,
                 web_url=excluded.web_url, loaded_at=excluded.loaded_at""",
            (
                source_id,
                project.external_id,
                project.name,
                project.full_path,
                project.web_url,
                loaded_at,
            ),
        )
        return int(
            connection.execute(
                "SELECT id FROM projects WHERE source_id=? AND external_id=?",
                (source_id, project.external_id),
            ).fetchone()[0]
        )

    @staticmethod
    def _upsert_work_item(
        connection: sqlite3.Connection,
        source_id: int,
        project_id: int,
        entry: TimeEntry,
        loaded_at: str,
    ) -> int | None:
        item = entry.work_item
        if item is None:
            return None
        connection.execute(
            """INSERT INTO work_items(
                   source_id, project_id, external_id, kind, iid,
                   title, web_url, branch_name, loaded_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, external_id) DO UPDATE SET
                 project_id=excluded.project_id, kind=excluded.kind, iid=excluded.iid,
                 title=excluded.title, web_url=excluded.web_url,
                 branch_name=excluded.branch_name, loaded_at=excluded.loaded_at""",
            (
                source_id,
                project_id,
                item.external_id,
                item.kind,
                item.iid,
                item.title,
                item.web_url,
                item.branch_name,
                loaded_at,
            ),
        )
        return int(
            connection.execute(
                "SELECT id FROM work_items WHERE source_id=? AND external_id=?",
                (source_id, item.external_id),
            ).fetchone()[0]
        )

    @staticmethod
    def _upsert_jira_links(
        connection: sqlite3.Connection,
        work_item_id: int,
        jira_source_id: int,
        title: str,
        branch_name: str | None,
        loaded_at: str,
    ) -> None:
        candidates = (("title", title, 0.9), ("branch", branch_name, 0.8))
        for location, text, confidence in candidates:
            if not text:
                continue
            for match in JIRA_KEY_RE.finditer(text):
                key = match.group(1).upper()
                connection.execute(
                    """INSERT INTO entity_links(
                           left_work_item_id, right_source_id, right_external_key,
                           method, confidence, evidence, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(
                           left_work_item_id, right_source_id,
                           right_external_key, method
                       )
                       DO UPDATE SET confidence=excluded.confidence,
                                     evidence=excluded.evidence,
                                     updated_at=excluded.updated_at""",
                    (
                        work_item_id,
                        jira_source_id,
                        key,
                        f"jira_key:{location}",
                        confidence,
                        match.group(0),
                        loaded_at,
                        loaded_at,
                    ),
                )

    @staticmethod
    def _persist_catalog_groups(
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
        loaded_at: str,
    ) -> dict[str, int]:
        for group in snapshot.groups:
            connection.execute(
                """INSERT INTO groups(
                       source_id, external_id, name, full_path, web_url,
                       loaded_at, raw_json, last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, external_id) DO UPDATE SET
                     name=excluded.name, full_path=excluded.full_path,
                     web_url=excluded.web_url, loaded_at=excluded.loaded_at,
                     raw_json=excluded.raw_json,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    group.external_id,
                    group.name,
                    group.full_path,
                    group.web_url,
                    loaded_at,
                    json.dumps(group.raw, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
        group_ids = Database._entity_ids(connection, "groups", source_id)
        for group in snapshot.groups:
            parent_id = group_ids.get(group.parent_external_id or "")
            connection.execute(
                "UPDATE groups SET parent_id=? WHERE source_id=? AND external_id=?",
                (parent_id, source_id, group.external_id),
            )
        return group_ids

    @staticmethod
    def _persist_catalog_projects(
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
        group_ids: dict[str, int],
        loaded_at: str,
    ) -> dict[str, int]:
        for project in snapshot.projects:
            connection.execute(
                """INSERT INTO projects(
                       source_id, external_id, name, full_path, web_url, loaded_at,
                       group_id, default_branch, archived, raw_json,
                       last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, external_id) DO UPDATE SET
                     name=excluded.name, full_path=excluded.full_path,
                     web_url=excluded.web_url, loaded_at=excluded.loaded_at,
                     group_id=excluded.group_id,
                     default_branch=excluded.default_branch,
                     archived=excluded.archived, raw_json=excluded.raw_json,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    project.external_id,
                    project.name,
                    project.full_path,
                    project.web_url,
                    loaded_at,
                    group_ids.get(project.group_external_id or ""),
                    project.default_branch,
                    int(project.archived),
                    json.dumps(project.raw, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
        return Database._entity_ids(connection, "projects", source_id)

    @staticmethod
    def _persist_catalog_memberships(
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
        group_ids: dict[str, int],
        project_ids: dict[str, int],
        loaded_at: str,
    ) -> None:
        for membership in snapshot.memberships:
            user = membership.user
            connection.execute(
                """INSERT INTO users(
                       source_id, external_id, username, display_name, web_url,
                       loaded_at, raw_json, last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, external_id) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     web_url=excluded.web_url, loaded_at=excluded.loaded_at,
                     raw_json=excluded.raw_json,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    user.external_id,
                    user.username,
                    user.display_name,
                    user.web_url,
                    loaded_at,
                    json.dumps(user.raw, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
            user_id = connection.execute(
                "SELECT id FROM users WHERE source_id=? AND external_id=?",
                (source_id, user.external_id),
            ).fetchone()[0]
            group_id = group_ids.get(membership.scope_external_id)
            project_id = project_ids.get(membership.scope_external_id)
            connection.execute(
                """INSERT INTO memberships(
                       source_id, external_id, scope_kind, group_id, project_id,
                       user_id, access_level, access_level_value, expires_at,
                       loaded_at, raw_json, last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, external_id) DO UPDATE SET
                     scope_kind=excluded.scope_kind, group_id=excluded.group_id,
                     project_id=excluded.project_id, user_id=excluded.user_id,
                     access_level=excluded.access_level,
                     access_level_value=excluded.access_level_value,
                     expires_at=excluded.expires_at,
                     loaded_at=excluded.loaded_at, raw_json=excluded.raw_json,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    membership.external_id,
                    membership.scope_kind,
                    group_id if membership.scope_kind == "group" else None,
                    project_id if membership.scope_kind == "project" else None,
                    user_id,
                    membership.access_level,
                    membership.access_level_value,
                    membership.expires_at,
                    loaded_at,
                    json.dumps(membership.raw, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )

    @staticmethod
    def _persist_catalog_work_items(
        connection: sqlite3.Connection,
        source_id: int,
        jira_source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
        group_ids: dict[str, int],
        project_ids: dict[str, int],
        loaded_at: str,
    ) -> None:
        for item in snapshot.work_items:
            connection.execute(
                """INSERT INTO work_items(
                       source_id, project_id, group_id, external_id, kind, iid,
                       title, web_url, branch_name, loaded_at, state, description,
                       created_at, updated_at, raw_json,
                       last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, external_id) DO UPDATE SET
                     project_id=excluded.project_id, group_id=excluded.group_id,
                     kind=excluded.kind, iid=excluded.iid,
                     title=excluded.title, web_url=excluded.web_url,
                     branch_name=excluded.branch_name,
                     loaded_at=excluded.loaded_at, state=excluded.state,
                     description=excluded.description,
                     created_at=excluded.created_at, updated_at=excluded.updated_at,
                     raw_json=excluded.raw_json,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    project_ids.get(item.project_external_id or ""),
                    group_ids.get(item.group_external_id or ""),
                    item.external_id,
                    item.kind,
                    item.iid,
                    item.title,
                    item.web_url,
                    item.branch_name,
                    loaded_at,
                    item.state,
                    item.description,
                    item.created_at,
                    item.updated_at,
                    json.dumps(item.raw, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
            work_item_id = connection.execute(
                "SELECT id FROM work_items WHERE source_id=? AND external_id=?",
                (source_id, item.external_id),
            ).fetchone()[0]
            Database._upsert_jira_links(
                connection,
                int(work_item_id),
                jira_source_id,
                item.title,
                item.branch_name,
                loaded_at,
            )

    @staticmethod
    def _persist_catalog_branches(
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
        project_ids: dict[str, int],
        loaded_at: str,
    ) -> None:
        for branch in snapshot.branches:
            project_id = project_ids[branch.project_external_id]
            connection.execute(
                """INSERT INTO branches(
                       source_id, project_id, name, is_default, loaded_at,
                       last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, project_id, name) DO UPDATE SET
                     is_default=excluded.is_default, loaded_at=excluded.loaded_at,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    project_id,
                    branch.name,
                    int(branch.is_default),
                    loaded_at,
                    run_id,
                ),
            )

    @staticmethod
    def _persist_catalog_iterations(
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        snapshot: CatalogSnapshot,
        group_ids: dict[str, int],
        loaded_at: str,
    ) -> None:
        for iteration in snapshot.iterations:
            connection.execute(
                """INSERT INTO iterations(
                       source_id, external_id, group_id, iid, title, description,
                       state, start_date, due_date, web_url, loaded_at, raw_json,
                       last_seen_catalog_run_id, is_active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source_id, external_id) DO UPDATE SET
                     group_id=excluded.group_id, iid=excluded.iid,
                     title=excluded.title, description=excluded.description,
                     state=excluded.state, start_date=excluded.start_date,
                     due_date=excluded.due_date, web_url=excluded.web_url,
                     loaded_at=excluded.loaded_at, raw_json=excluded.raw_json,
                     last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
                     is_active=1""",
                (
                    source_id,
                    iteration.external_id,
                    group_ids[iteration.group_external_id],
                    iteration.iid,
                    iteration.title,
                    iteration.description,
                    iteration.state,
                    iteration.start_date,
                    iteration.due_date,
                    iteration.web_url,
                    loaded_at,
                    json.dumps(iteration.raw, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )

    @staticmethod
    def _deactivate_missing_catalog(
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        incomplete_tables: set[str],
    ) -> None:
        for table in (
            "groups",
            "projects",
            "users",
            "work_items",
            "memberships",
            "branches",
            "iterations",
        ):
            if table in incomplete_tables:
                continue
            connection.execute(
                f"""UPDATE {table} SET is_active=0
                    WHERE source_id=?
                      AND last_seen_catalog_run_id IS NOT NULL
                      AND last_seen_catalog_run_id<>?""",
                (source_id, run_id),
            )

    @staticmethod
    def _entity_ids(
        connection: sqlite3.Connection, table: str, source_id: int
    ) -> dict[str, int]:
        rows = connection.execute(
            f"SELECT external_id, id FROM {table} WHERE source_id=?",
            (source_id,),
        )
        return {str(row[0]): int(row[1]) for row in rows}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
