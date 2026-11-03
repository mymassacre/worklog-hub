from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GroupScope:
    paths: tuple[str, ...]
    include_subgroups: bool = True

    def contains(self, group_path: str) -> bool:
        return any(
            group_path == root
            or (self.include_subgroups and group_path.startswith(root + "/"))
            for root in self.paths
        )


@dataclass(frozen=True, slots=True)
class GitLabConfig:
    base_url: str
    token: str = field(repr=False)
    timeout_seconds: float = 30.0
    scope: GroupScope = field(default_factory=lambda: GroupScope(()))

    @property
    def graphql_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/graphql"


@dataclass(frozen=True, slots=True)
class ProjectScope:
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JiraConfig:
    base_url: str
    token: str = field(repr=False)
    project_keys: tuple[str, ...] = ()
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    gitlab_url: str
    scope: GroupScope
    timeout_seconds: float = 30.0
    jira_url: str = ""
    jira_projects: tuple[str, ...] = ()
    jira_timeout_seconds: float = 30.0

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        require_groups: bool = True,
        source: str | None = "gitlab",
        require_jira_projects: bool = False,
    ) -> AppConfig:
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(
                f"Не удалось прочитать TOML-конфиг: {path}"
            ) from exc
        _check_keys(raw, {"database", "gitlab", "jira"}, "конфига")
        database = _table(raw, "database")
        gitlab = _table(raw, "gitlab")
        _check_keys(database, {"path"}, "database")
        _check_keys(
            gitlab, {"url", "groups", "include_subgroups", "timeout_seconds"}, "gitlab"
        )
        url = _url(gitlab, "gitlab") if gitlab or source == "gitlab" else ""
        groups = gitlab.get("groups", [])
        if not isinstance(groups, list) or (require_groups and not groups):
            raise ConfigurationError(
                "gitlab.groups: укажите непустой список путей групп"
            )
        paths: list[str] = []
        for group in groups:
            if (
                not isinstance(group, str)
                or not group
                or any(not part or part in {".", ".."} for part in group.split("/"))
                or any(char.isspace() or char in ":\\?#%*" for char in group)
            ):
                raise ConfigurationError(
                    "gitlab.groups: нужны полные пути, например team/backend"
                )
            if group not in paths:
                paths.append(group)
        include = gitlab.get("include_subgroups", True)
        if not isinstance(include, bool):
            raise ConfigurationError(
                "gitlab.include_subgroups должен быть true или false"
            )
        timeout = _timeout(gitlab, "gitlab")
        jira = _table(raw, "jira")
        _check_keys(jira, {"url", "projects", "timeout_seconds"}, "jira")
        jira_url = _url(jira, "jira") if jira or source == "jira" else ""
        projects = jira.get("projects", [])
        if (
            not isinstance(projects, list)
            or (require_jira_projects and not projects)
            or any(
                not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                for key in projects
            )
        ):
            raise ConfigurationError(
                'jira.projects: нужны ключи проектов, например ["ABC"]'
            )
        db_path = database.get("path", "worklog-hub.sqlite3")
        if not isinstance(db_path, str) or not db_path.strip():
            raise ConfigurationError("database.path должен быть непустым путём")
        resolved = Path(db_path)
        if not resolved.is_absolute():
            resolved = path.resolve().parent / resolved
        return cls(
            resolved,
            url,
            GroupScope(tuple(paths), include),
            timeout,
            jira_url,
            tuple(dict.fromkeys(projects)),
            _timeout(jira, "jira"),
        )

    def gitlab_config(self, token: str) -> GitLabConfig:
        return GitLabConfig(self.gitlab_url, token, self.timeout_seconds, self.scope)

    def jira_config(self, token: str) -> JiraConfig:
        return JiraConfig(
            self.jira_url, token, self.jira_projects, self.jira_timeout_seconds
        )


def _url(raw: dict, section: str) -> str:
    url = raw.get("url")
    if not isinstance(url, str):
        raise ConfigurationError(f"{section}.url должен быть URL сервера")
    url = url.strip().rstrip("/")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
            and not any(char.isspace() for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(
            f"{section}.url должен быть HTTP(S) URL без учётных данных"
        )
    return url


def _timeout(raw: dict, section: str) -> float:
    timeout = raw.get("timeout_seconds", 30.0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ConfigurationError(
            f"{section}.timeout_seconds должен быть положительным числом"
        )
    return float(timeout)


def _table(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} должен быть TOML-секцией")
    return value


def _check_keys(raw: dict, allowed: set[str], section: str) -> None:
    if set(raw) - allowed:
        raise ConfigurationError(
            f"Неизвестные поля секции {section}. Токен хранится только в keyring"
        )
