from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from datetime import date
from pathlib import Path

from worklog_hub.adapters.gitlab import GitLabCollector, GitLabGraphQLClient
from worklog_hub.adapters.gitlab_catalog import GitLabCatalogCollector
from worklog_hub.adapters.jira import JiraCollector, JiraRESTClient
from worklog_hub.config import AppConfig, ConfigurationError, ProjectScope
from worklog_hub.credentials import load_token, save_token
from worklog_hub.database import Database
from worklog_hub.mapping import KINDS, STATUSES, MappingService
from worklog_hub.security import redact_secrets
from worklog_hub.sync import synchronize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worklog-hub", description="Сбор фактических трудозатрат"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("worklog-hub.toml"),
        help="Путь TOML-конфига",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    gitlab = commands.add_parser("gitlab", help="Команды GitLab")
    gitlab_commands = gitlab.add_subparsers(dest="gitlab_command", required=True)
    gitlab_commands.add_parser("check", help="Проверить подключение")
    gitlab_commands.add_parser("login", help="Сохранить токен в системный keyring")
    groups_parser = gitlab_commands.add_parser(
        "groups", help="Показать доступные группы для заполнения конфига"
    )
    groups_parser.add_argument("--search", help="Поиск по названию или полному пути")
    sync_parser = gitlab_commands.add_parser("sync", help="Синхронизировать период")
    sync_parser.add_argument(
        "--from", dest="date_from", type=date.fromisoformat, required=True
    )
    sync_parser.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, required=True
    )
    jira = commands.add_parser("jira", help="Чтение Jira Server/Data Center REST API")
    jira_commands = jira.add_subparsers(dest="jira_command", required=True)
    jira_commands.add_parser("login", help="Сохранить PAT Jira в keyring")
    jira_commands.add_parser("check", help="Проверить доступ с PAT")
    jira_commands.add_parser("projects", help="Показать доступные проекты")
    jira_sync = jira_commands.add_parser(
        "sync", help="Собрать worklog выбранных проектов"
    )
    jira_sync.add_argument(
        "--from", dest="date_from", type=date.fromisoformat, required=True
    )
    jira_sync.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, required=True
    )
    commands.add_parser("sync-status", help="Показать последние запуски")
    mapping = commands.add_parser("mapping", help="Локальные связи GitLab ↔ Jira")
    mapping_commands = mapping.add_subparsers(dest="mapping_command", required=True)
    entities = mapping_commands.add_parser(
        "entities", help="Показать загруженные сущности"
    )
    entities.add_argument("kind", choices=KINDS)
    entities.add_argument("--source", required=True, choices=("gitlab", "jira"))
    link = mapping_commands.add_parser(
        "link", help="Задать подтверждённое соответствие"
    )
    link.add_argument("kind", choices=KINDS)
    link.add_argument("--gitlab", required=True, help="Селектор или внешний ID GitLab")
    link.add_argument("--jira", required=True, help="Селектор или внешний ID Jira")
    suggest = mapping_commands.add_parser(
        "suggest", help="Предложить связи задач и пользователей"
    )
    suggest.add_argument("--kind", choices=("task", "user"))
    listing = mapping_commands.add_parser("list", help="Показать связи и кандидатов")
    listing.add_argument("--kind", choices=KINDS)
    listing.add_argument("--status", choices=STATUSES)
    for action in ("confirm", "reject"):
        review = mapping_commands.add_parser(
            action, help="Подтвердить или отклонить связь"
        )
        review.add_argument("id", type=int, help="Локальный ID связи из mapping list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secrets: tuple[str, ...] = ()
    try:
        require_groups = args.command == "gitlab" and args.gitlab_command in {
            "check",
            "sync",
        }
        source = args.command if args.command in {"gitlab", "jira"} else None
        app_config = AppConfig.from_file(
            args.config,
            require_groups=require_groups,
            source=source,
            require_jira_projects=args.command == "jira"
            and args.jira_command == "sync",
        )
        database = Database(app_config.database_path)
        if args.command == "mapping":
            database.migrate()
            _run_mapping(args, MappingService(database))
            return 0
        if args.command == "sync-status":
            database.migrate()
            _print_status(database)
            return 0

        if args.command == "jira":
            if args.jira_command == "login":
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    token = getpass.getpass("PAT Jira (ввод скрыт): ")
                save_token(app_config.jira_url, token, source="jira")
                print("Токен Jira сохранён в системном keyring")
                return 0
            token = load_token(app_config.jira_url, source="jira")
            secrets = (token,)
            jira_config = app_config.jira_config(token)
            jira_collector = JiraCollector(JiraRESTClient(jira_config))
            if args.jira_command == "check":
                username = jira_collector.check()
                print(f"Jira REST API доступен, пользователь: {username}")
                return 0
            if args.jira_command == "projects":
                projects = jira_collector.list_projects()
                print("Ключ\tНазвание\tID")
                for project in projects:
                    print(f"{project['key']}\t{project['name']}\t{project['id']}")
                print(
                    f"Всего проектов: {len(projects)}. Ключи задаются в jira.projects."
                )
                return 0
            result = synchronize(
                database,
                jira_collector,
                args.date_from,
                args.date_to,
                catalog_collector=jira_collector,
                source_url=jira_config.base_url,
                secrets=secrets,
                scope=ProjectScope(jira_config.project_keys),
                reconcile_missing=False,
            )
            print(f"Синхронизация Jira #{result.run_id}: получено {result.fetched}")
            for warning in result.warnings:
                print(f"Предупреждение: {warning}", file=sys.stderr)
            return 0

        if args.gitlab_command == "login":
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Токен GitLab (ввод скрыт): ")
            save_token(app_config.gitlab_url, token)
            print("Токен сохранён в системном keyring")
            return 0
        token = load_token(app_config.gitlab_url)
        secrets = (token,)
        gitlab_config = app_config.gitlab_config(token)
        client = GitLabGraphQLClient(gitlab_config)
        catalog = GitLabCatalogCollector(client)
        if args.gitlab_command == "groups":
            groups = catalog.list_available_groups(args.search)
            if not groups:
                print("Доступных групп не найдено")
                return 0
            print("Путь группы\tНазвание\tID\tURL")
            for group in groups:
                print(
                    f"{group.full_path}\t{group.name}\t{group.external_id}\t"
                    f"{group.web_url or ''}"
                )
            print(
                f"Всего групп: {len(groups)}. "
                "Для gitlab.groups используйте полные пути."
            )
            return 0
        collector = GitLabCollector(client, catalog)
        if args.gitlab_command == "check":
            username = collector.check()
            groups = catalog.collect_groups()
            print(f"GitLab доступен, пользователь: {username}")
            print("Группы: " + ", ".join(group.full_path for group in groups))
            return 0
        result = synchronize(
            database,
            collector,
            args.date_from,
            args.date_to,
            catalog_collector=catalog,
            source_url=gitlab_config.base_url,
            secrets=(gitlab_config.token,),
            scope=app_config.scope,
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
        for warning in result.warnings:
            print(f"Предупреждение: {warning}", file=sys.stderr)
        return 0
    except (getpass.GetPassWarning, EOFError):
        print(
            "Ошибка: для скрытого ввода токена нужен интерактивный терминал",
            file=sys.stderr,
        )
        return 1
    except (ConfigurationError, ValueError, RuntimeError, OSError) as exc:
        print(f"Ошибка: {redact_secrets(str(exc), secrets)}", file=sys.stderr)
        return 1


def _run_mapping(args: argparse.Namespace, service: MappingService) -> None:
    if args.mapping_command == "entities":
        rows = service.entities(args.kind, args.source)
        print("Селектор\tНазвание\tВнешний ID\tАктивна")
        for row in rows:
            print(
                f"{row['selector']}\t{row['label']}\t{row['external_id']}\t{row['is_active']}"
            )
        print(f"Всего: {len(rows)}")
    elif args.mapping_command == "link":
        mapping_id = service.link(args.kind, args.gitlab, args.jira)
        print(f"Связь #{mapping_id} подтверждена")
    elif args.mapping_command == "suggest":
        count = service.suggest(args.kind)
        print(f"Новых предложений: {count}. Посмотреть: mapping list --status proposed")
    elif args.mapping_command in {"confirm", "reject"}:
        status = "confirmed" if args.mapping_command == "confirm" else "rejected"
        service.review(args.id, status)
        print(f"Связь #{args.id}: {status}")
    else:
        print("ID\tТип\tСтатус\tGitLab\tJira\tАктуальна\tОснование")
        for row in service.list(args.kind, args.status):
            current = row["is_current"] and row["gitlab_active"] and row["jira_active"]
            print(
                f"{row['id']}\t{row['kind']}\t{row['status']}\t{row['gitlab']}\t"
                f"{row['jira']}\t{int(bool(current))}\t{row['evidence']}"
            )


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
        if row["warnings_json"]:
            details += f" warnings={row['warnings_json']}"
        print(details)
