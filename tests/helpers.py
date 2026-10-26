from __future__ import annotations

from copy import deepcopy
from typing import Any


def gitlab_timelog(
    external_id: str = "gid://gitlab/Timelog/1",
    *,
    seconds: int = 3600,
    spent_at: str = "2026-10-10T17:30:00+07:00",
    title: str = "ABC-123 Сделать отчёт",
) -> dict[str, Any]:
    value = {
        "id": external_id,
        "spentAt": spent_at,
        "timeSpent": seconds,
        "summary": "Работа",
        "user": {
            "id": "gid://gitlab/User/7",
            "username": "ivan",
            "name": "Иван",
            "webUrl": "https://gitlab.example/ivan",
        },
        "project": {
            "id": "gid://gitlab/Project/2",
            "name": "Demo",
            "fullPath": "group/demo",
            "webUrl": "https://gitlab.example/group/demo",
        },
        "issue": {
            "id": "gid://gitlab/Issue/3",
            "iid": "12",
            "title": title,
            "webUrl": "https://gitlab.example/group/demo/-/issues/12",
        },
        "mergeRequest": None,
    }
    return deepcopy(value)
