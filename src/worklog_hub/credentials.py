from __future__ import annotations

import sys

import keyring
from keyring.backend import KeyringBackend

from worklog_hub.config import ConfigurationError

SERVICE_NAME = "worklog-hub/gitlab"


def _backend() -> KeyringBackend:
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    backend = keyring.get_keyring()
    candidates = getattr(backend, "backends", [backend])
    for candidate in candidates:
        if type(candidate).__module__ in {
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
        }:
            return candidate
    raise ConfigurationError(
        "Системный keyring недоступен; файловые хранилища не используются"
    )


def load_token(base_url: str, *, source: str = "gitlab") -> str:
    try:
        token = _backend().get_password(_service(source), base_url)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "Не удалось прочитать токен из системного keyring"
        ) from exc
    if not token or not token.strip():
        raise ConfigurationError(
            f"Токен не найден в keyring. Выполните: worklog-hub {source} login"
        )
    return token


def save_token(base_url: str, token: str, *, source: str = "gitlab") -> None:
    if not token.strip():
        raise ConfigurationError("Токен не может быть пустым")
    try:
        _backend().set_password(_service(source), base_url, token.strip())
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "Не удалось сохранить токен в системный keyring"
        ) from exc


def _service(source: str) -> str:
    if source not in {"gitlab", "jira"}:
        raise ConfigurationError("Неизвестный источник токена")
    return f"worklog-hub/{source}"
