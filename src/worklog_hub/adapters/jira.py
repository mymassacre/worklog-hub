from __future__ import annotations

from datetime import date

from worklog_hub.domain import TimeEntry


class JiraCollector:
    """Контракт будущего адаптера внутреннего API Jira.

    Сетевой код намеренно отсутствует до получения обезличенных примеров
    запросов веб-клиента из DevTools.
    """

    source_name = "jira"

    def check(self) -> str:
        raise NotImplementedError(
            "Jira API ещё не настроен: нужны примеры запросов из DevTools"
        )

    def collect(self, date_from: date, date_to: date) -> list[TimeEntry]:
        raise NotImplementedError(
            "Jira API ещё не настроен: нужны примеры запросов из DevTools"
        )
