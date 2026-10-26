from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from worklog_hub.database import Database
from worklog_hub.domain import CatalogCollector, Collector
from worklog_hub.security import redact_secrets


@dataclass(frozen=True, slots=True)
class SyncResult:
    run_id: int
    fetched: int
    deleted: int
    catalog_counts: dict[str, int]
    warnings: list[str]


def synchronize(
    database: Database,
    collector: Collector,
    date_from: date,
    date_to: date,
    *,
    catalog_collector: CatalogCollector | None = None,
    source_url: str | None = None,
    secrets: tuple[str, ...] = (),
) -> SyncResult:
    if date_from > date_to:
        raise ValueError("Начальная дата позже конечной")
    database.migrate()
    source_id = database.ensure_source(collector.source_name, source_url)
    jira_source_id = database.ensure_source("jira")
    run_id = database.start_run(source_id, date_from, date_to)
    try:
        snapshot = (
            catalog_collector.collect_catalog()
            if catalog_collector is not None
            else None
        )
        entries = collector.collect(date_from, date_to)
        catalog_counts: dict[str, int] = {}
        if snapshot is not None:
            catalog_counts = database.persist_catalog(
                source_id, jira_source_id, run_id, snapshot
            )
        deleted = database.persist_entries(
            source_id, jira_source_id, run_id, date_from, date_to, entries
        )
        return SyncResult(
            run_id=run_id,
            fetched=len(entries),
            deleted=deleted,
            catalog_counts=catalog_counts,
            warnings=snapshot.warnings if snapshot is not None else [],
        )
    except Exception as exc:
        safe_message = redact_secrets(str(exc), secrets)
        database.fail_run(run_id, safe_message)
        raise
