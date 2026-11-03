from __future__ import annotations

import json
import sqlite3
from typing import Any

from worklog_hub.database import JIRA_KEY_RE, Database, utc_now

KINDS = {
    "project": ("projects", "project_id"),
    "task": ("work_items", "work_item_id"),
    "user": ("users", "user_id"),
}
STATUSES = ("proposed", "confirmed", "rejected")


class MappingError(ValueError):
    pass


class MappingService:
    """Локальные связи. Сущности и списания исходных систем не объединяются."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def entities(self, kind: str, source: str) -> list[dict[str, Any]]:
        self._kind(kind)
        if source not in {"gitlab", "jira"}:
            raise MappingError("Источник должен быть gitlab или jira")
        with self.database.connect() as connection:
            return self._entities(connection, kind, source)

    @staticmethod
    def _kind(kind: str) -> tuple[str, str]:
        if kind not in KINDS:
            raise MappingError("Тип связи должен быть project, task или user")
        return KINDS[kind]

    @staticmethod
    def _entities(
        connection: sqlite3.Connection, kind: str, source: str
    ) -> list[dict[str, Any]]:
        table, _ = MappingService._kind(kind)
        if kind == "task":
            query = """SELECT e.*, p.full_path AS project_path
                       FROM work_items e JOIN sources s ON s.id=e.source_id
                       LEFT JOIN projects p ON p.id=e.project_id WHERE s.name=?"""
        else:
            query = (
                f"SELECT e.* FROM {table} e "
                "JOIN sources s ON s.id=e.source_id WHERE s.name=?"
            )
        rows = [dict(row) for row in connection.execute(query, (source,))]
        for row in rows:
            if kind == "project":
                row["selector"] = row["full_path"] or row["external_id"]
                row["label"] = row["name"]
            elif kind == "user":
                row["selector"] = row["username"]
                row["label"] = row["display_name"] or row["username"]
            else:
                marker = "!" if row["kind"] == "merge_request" else "#"
                row["selector"] = (
                    f"{row['project_path']}{marker}{row['iid']}"
                    if source == "gitlab" and row["project_path"] and row["iid"]
                    else row["iid"] or row["external_id"]
                )
                row["label"] = row["title"]
        return sorted(rows, key=lambda row: (row["selector"], row["external_id"]))

    def _resolve(
        self, connection: sqlite3.Connection, kind: str, source: str, selector: str
    ) -> dict[str, Any]:
        rows = self._entities(connection, kind, source)
        matches = [
            row
            for row in rows
            if selector
            in {
                row["external_id"],
                row["selector"],
                row.get("web_url"),
            }
        ]
        if len(matches) != 1:
            raise MappingError(
                f"Для {source} {selector!r} найдено сущностей: {len(matches)}. "
                "Посмотрите mapping entities; при неоднозначности задайте внешний ID"
            )
        if not matches[0]["is_active"]:
            raise MappingError("Выбранная сущность неактивна; проверьте синхронизацию")
        return matches[0]

    def link(self, kind: str, gitlab: str, jira: str) -> int:
        _, suffix = self._kind(kind)
        with self.database.connect() as connection:
            left = self._resolve(connection, kind, "gitlab", gitlab)
            right = self._resolve(connection, kind, "jira", jira)
            with connection:
                return self._store(
                    connection,
                    kind,
                    suffix,
                    left["id"],
                    right["id"],
                    "confirmed",
                    "manual",
                    "Явное соответствие, заданное пользователем",
                )

    @staticmethod
    def _store(
        connection: sqlite3.Connection,
        kind: str,
        suffix: str,
        left: int,
        right: int,
        status: str,
        method: str,
        evidence: str,
    ) -> int:
        left_column, right_column = f"gitlab_{suffix}", f"jira_{suffix}"
        old = connection.execute(
            f"SELECT * FROM entity_mappings WHERE {left_column}=? AND {right_column}=?",
            (left, right),
        ).fetchone()
        if old is not None and method != "manual" and old["status"] != "proposed":
            return int(old["id"])
        now = utc_now()
        try:
            connection.execute(
                f"""INSERT INTO entity_mappings(
                       kind, {left_column}, {right_column}, status, method, evidence,
                       is_current, created_at, updated_at, reviewed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT({left_column}, {right_column}) DO UPDATE SET
                     status=excluded.status, method=excluded.method,
                     evidence=excluded.evidence, is_current=1,
                     updated_at=excluded.updated_at,
                     reviewed_at=excluded.reviewed_at""",
                (
                    kind,
                    left,
                    right,
                    status,
                    method,
                    evidence,
                    now,
                    now,
                    now if status == "confirmed" else None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MappingError(
                "Аккаунт уже связан с другим пользователем; "
                "сначала отклоните прежнюю связь"
            ) from exc
        mapping_id = int(
            connection.execute(
                f"SELECT id FROM entity_mappings "
                f"WHERE {left_column}=? AND {right_column}=?",
                (left, right),
            ).fetchone()[0]
        )
        if old is None or old["status"] != status or method == "manual":
            connection.execute(
                "INSERT INTO mapping_reviews("
                "mapping_id, previous_status, new_status, action, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    mapping_id,
                    old["status"] if old is not None else None,
                    status,
                    method,
                    now,
                ),
            )
        return mapping_id

    @staticmethod
    def _keys(item: dict[str, Any]) -> dict[str, list[str]]:
        fields = {"title": item["title"], "branch": item.get("branch_name")}
        if item.get("raw_json"):
            raw = json.loads(item["raw_json"])
            if isinstance(raw, dict) and isinstance(raw.get("description"), str):
                fields["description"] = raw["description"]
        found: dict[str, list[str]] = {}
        for location, value in fields.items():
            for match in JIRA_KEY_RE.finditer(value or ""):
                key = match.group(1).upper()
                found.setdefault(key, [])
                evidence = f"{location}: {key}"
                if evidence not in found[key]:
                    found[key].append(evidence)
        return found

    @staticmethod
    def _project_pairs(connection: sqlite3.Connection) -> set[tuple[int, int]]:
        return {
            tuple(row)
            for row in connection.execute(
                """SELECT m.gitlab_project_id, m.jira_project_id FROM entity_mappings m
               JOIN projects g ON g.id=m.gitlab_project_id
               JOIN projects j ON j.id=m.jira_project_id
               WHERE m.kind='project' AND m.status='confirmed'
                 AND g.is_active=1 AND j.is_active=1"""
            )
        }

    def suggest(self, kind: str | None = None) -> int:
        if kind not in {None, "task", "user"}:
            raise MappingError(
                "Автопоиск поддерживает task и user; проекты связываются вручную"
            )
        with self.database.connect() as connection, connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM entity_mappings"
            ).fetchone()[0]
            for current_kind in ("task", "user"):
                if kind is not None and kind != current_kind:
                    continue
                connection.execute(
                    "UPDATE entity_mappings SET is_current=0 "
                    "WHERE kind=? AND status='proposed'",
                    (current_kind,),
                )
                left_rows = [
                    row
                    for row in self._entities(connection, current_kind, "gitlab")
                    if row["is_active"]
                ]
                right_rows = [
                    row
                    for row in self._entities(connection, current_kind, "jira")
                    if row["is_active"]
                ]
                if current_kind == "task":
                    pairs = self._project_pairs(connection)
                    by_key: dict[str, list[dict[str, Any]]] = {}
                    for row in right_rows:
                        by_key.setdefault((row["iid"] or "").upper(), []).append(row)
                    for left in left_rows:
                        for key, evidence in self._keys(left).items():
                            for right in by_key.get(key, []):
                                if (left["project_id"], right["project_id"]) in pairs:
                                    self._store(
                                        connection,
                                        "task",
                                        "work_item_id",
                                        left["id"],
                                        right["id"],
                                        "proposed",
                                        "jira_key",
                                        "; ".join(evidence),
                                    )
                else:
                    by_username: dict[str, list[dict[str, Any]]] = {}
                    for right in right_rows:
                        by_username.setdefault(right["username"].casefold(), []).append(
                            right
                        )
                    for left in left_rows:
                        for right in by_username.get(left["username"].casefold(), []):
                            self._store(
                                connection,
                                "user",
                                "user_id",
                                left["id"],
                                right["id"],
                                "proposed",
                                "username",
                                "Совпадает username; личность требует проверки",
                            )
            after = connection.execute(
                "SELECT COUNT(*) FROM entity_mappings"
            ).fetchone()[0]
            return int(after - before)

    def review(self, mapping_id: int, status: str) -> None:
        if status not in {"confirmed", "rejected"}:
            raise MappingError("Допустимы только confirmed или rejected")
        with self.database.connect() as connection, connection:
            row = connection.execute(
                "SELECT * FROM entity_mappings WHERE id=?", (mapping_id,)
            ).fetchone()
            if row is None:
                raise MappingError("Связь не найдена")
            if status == "confirmed":
                self._validate_confirmation(connection, dict(row))
            if row["status"] == status:
                return
            now = utc_now()
            try:
                connection.execute(
                    "UPDATE entity_mappings SET status=?, updated_at=?, reviewed_at=? "
                    "WHERE id=?",
                    (status, now, now, mapping_id),
                )
            except sqlite3.IntegrityError as exc:
                raise MappingError("Аккаунт уже связан с другим пользователем") from exc
            connection.execute(
                "INSERT INTO mapping_reviews("
                "mapping_id, previous_status, new_status, action, created_at) "
                "VALUES (?, ?, ?, 'review', ?)",
                (mapping_id, row["status"], status, now),
            )

    def _validate_confirmation(
        self,
        connection: sqlite3.Connection,
        mapping: dict[str, Any],
        entities: dict | None = None,
        pairs: set[tuple[int, int]] | None = None,
    ) -> None:
        kind = mapping["kind"]
        _, suffix = self._kind(kind)
        if entities is None:
            entities = {
                (kind, source): {
                    row["id"]: row for row in self._entities(connection, kind, source)
                }
                for source in ("gitlab", "jira")
            }
        left = entities[(kind, "gitlab")].get(mapping[f"gitlab_{suffix}"])
        right = entities[(kind, "jira")].get(mapping[f"jira_{suffix}"])
        if (
            left is None
            or right is None
            or not left["is_active"]
            or not right["is_active"]
        ):
            raise MappingError("Сущность недоступна или неактивна; обновите данные")
        if mapping["status"] == "confirmed" or mapping["method"] == "manual":
            return
        if not mapping["is_current"]:
            raise MappingError("Предложение устарело; запустите mapping suggest заново")
        if kind == "task":
            if pairs is None:
                pairs = self._project_pairs(connection)
            if (left["project_id"], right["project_id"]) not in pairs:
                raise MappingError("Соответствие проектов больше не подтверждено")
            if (right["iid"] or "").upper() not in self._keys(left):
                raise MappingError(
                    "Ключ Jira больше не найден; задайте ручную связь при необходимости"
                )
        if (
            kind == "user"
            and left["username"].casefold() != right["username"].casefold()
        ):
            raise MappingError(
                "Username изменился; задайте ручную связь при необходимости"
            )

    def list(
        self, kind: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        if kind is not None:
            self._kind(kind)
        if status is not None and status not in STATUSES:
            raise MappingError("Неизвестный статус связи")
        with self.database.connect() as connection:
            pairs = self._project_pairs(connection)
            entities = {
                (k, source): {r["id"]: r for r in self._entities(connection, k, source)}
                for k in KINDS
                for source in ("gitlab", "jira")
            }
            rows = connection.execute("SELECT * FROM entity_mappings ORDER BY kind, id")
            result = []
            for row in rows:
                if (kind is not None and row["kind"] != kind) or (
                    status is not None and row["status"] != status
                ):
                    continue
                record = dict(row)
                if record["status"] == "proposed":
                    try:
                        self._validate_confirmation(connection, record, entities, pairs)
                    except MappingError:
                        record["is_current"] = 0
                _, suffix = self._kind(row["kind"])
                for source in ("gitlab", "jira"):
                    entity = entities[(row["kind"], source)][row[f"{source}_{suffix}"]]
                    record[source] = entity["selector"]
                    record[f"{source}_active"] = bool(entity["is_active"])
                result.append(record)
            return result
