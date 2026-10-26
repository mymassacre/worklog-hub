from __future__ import annotations

import argparse
import sys
from contextlib import suppress
from datetime import date

from worklog_hub.adapters.gitlab import GitLabCollector, GitLabGraphQLClient
from worklog_hub.adapters.gitlab_catalog import GitLabCatalogCollector
from worklog_hub.config import AppConfig, ConfigurationError, GitLabConfig
from worklog_hub.database import Database
from worklog_hub.security import redact_secrets
from worklog_hub.sync import synchronize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worklog-hub", description="Сбор фактических трудозатрат"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    gitlab = commands.add_parser("gitlab", help="Команды GitLab")
    gitlab_commands = gitlab.add_subparsers(dest="gitlab_command", required=True)
    gitlab_commands.add_parser("check", help="Проверить подключение")
    sync_parser = gitlab_commands.add_parser("sync", help="Синхронизировать период")
    sync_parser.add_argument(
        "--from", dest="date_from", type=date.fromisoformat, required=True
    )
    sync_parser.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, required=True
    )
    commands.add_parser("sync-status", help="Показать последние запуски")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app_config = AppConfig.from_env()
    database = Database(app_config.database_path)
    try:
        if args.command == "sync-status":
            database.migrate()
            _print_status(database)
            return 0

        gitlab_config = GitLabConfig.from_env()
        client = GitLabGraphQLClient(gitlab_config)
        collector = GitLabCollector(client)
        if args.gitlab_command == "check":
            username = collector.check()
            print(f"GitLab доступен, пользователь: {username}")
            return 0
        result = synchronize(
            database,
            collector,
            args.date_from,
            args.date_to,
            catalog_collector=GitLabCatalogCollector(client),
            source_url=gitlab_config.base_url,
            secrets=(gitlab_config.token,),
        )
        print(
            f"Синхронизация #{result.run_id}: получено {result.fetched}, "
            f"удалено локально {result.deleted}"
        )
        if result.catalog_counts:
            counts = ", ".join(
                f"{name}={count}"
                for name, count in sorted(result.catalog_counts.items())
            )
            print(f"Каталог GitLab: {counts}")
        return 0
    except (ConfigurationError, ValueError, RuntimeError) as exc:
        secrets: tuple[str, ...] = ()
        with suppress(ConfigurationError):
            secrets = (GitLabConfig.from_env().token,)
        print(f"Ошибка: {redact_secrets(str(exc), secrets)}", file=sys.stderr)
        return 1


def _print_status(database: Database) -> None:
    rows = database.recent_runs()
    if not rows:
        print("Запусков синхронизации пока нет")
        return
    for row in rows:
        details = (
            f"#{row['id']} {row['source']} {row['date_from']}..{row['date_to']} "
            f"{row['status']} fetched={row['fetched_count']} "
            f"upserted={row['upserted_count']} deleted={row['deleted_count']}"
        )
        if row["error_message"]:
            details += f" error={row['error_message']}"
        if row["catalog_counts_json"]:
            details += f" catalog={row['catalog_counts_json']}"
        print(details)
