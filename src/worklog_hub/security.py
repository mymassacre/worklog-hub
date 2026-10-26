from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"(?i)(private-token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)\S+"),
)


def redact_secrets(message: str, secrets: tuple[str, ...] = ()) -> str:
    result = message
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(r"\1***", result)
    return result
