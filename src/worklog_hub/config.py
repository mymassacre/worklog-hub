from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            database_path=Path(os.getenv("WORKLOG_DB_PATH", "worklog-hub.sqlite3"))
        )


@dataclass(frozen=True, slots=True)
class GitLabConfig:
    base_url: str
    token: str
    timeout_seconds: float = 30.0

    @property
    def graphql_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/graphql"

    @classmethod
    def from_env(cls) -> GitLabConfig:
        base_url = os.getenv("GITLAB_URL", "").strip()
        token = os.getenv("GITLAB_TOKEN", "").strip()
        missing = [
            name
            for name, value in (("GITLAB_URL", base_url), ("GITLAB_TOKEN", token))
            if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Не заданы переменные окружения: {', '.join(missing)}"
            )
        try:
            timeout = float(os.getenv("GITLAB_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise ConfigurationError(
                "GITLAB_TIMEOUT_SECONDS должен быть числом"
            ) from exc
        if timeout <= 0:
            raise ConfigurationError("GITLAB_TIMEOUT_SECONDS должен быть больше нуля")
        return cls(base_url=base_url, token=token, timeout_seconds=timeout)
