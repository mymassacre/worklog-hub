from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Person:
    external_id: str
    username: str
    display_name: str | None = None
    web_url: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    external_id: str
    name: str
    full_path: str | None = None
    web_url: str | None = None


@dataclass(frozen=True, slots=True)
class WorkItem:
    external_id: str
    kind: str
    title: str
    iid: str | None = None
    web_url: str | None = None
    branch_name: str | None = None


@dataclass(frozen=True, slots=True)
class TimeEntry:
    external_id: str
    user: Person
    project: Project
    work_item: WorkItem | None
    spent_at: datetime
    spent_date: date
    seconds: int
    summary: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogGroup:
    external_id: str
    name: str
    full_path: str
    web_url: str | None
    parent_external_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogProject:
    external_id: str
    name: str
    full_path: str
    web_url: str | None
    group_external_id: str | None
    default_branch: str | None
    archived: bool
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogUser:
    external_id: str
    username: str
    display_name: str | None
    web_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogMembership:
    external_id: str
    scope_kind: str
    scope_external_id: str
    user: CatalogUser
    access_level: str | None
    access_level_value: int | None
    expires_at: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogWorkItem:
    external_id: str
    kind: str
    iid: str
    title: str
    description: str | None
    state: str | None
    web_url: str | None
    project_external_id: str | None
    group_external_id: str | None
    branch_name: str | None
    created_at: str | None
    updated_at: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogBranch:
    project_external_id: str
    name: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class CatalogIteration:
    external_id: str
    group_external_id: str
    iid: str
    title: str | None
    description: str | None
    state: str
    start_date: str | None
    due_date: str | None
    web_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    groups: list[CatalogGroup]
    projects: list[CatalogProject]
    memberships: list[CatalogMembership]
    work_items: list[CatalogWorkItem]
    branches: list[CatalogBranch]
    iterations: list[CatalogIteration]
    warnings: list[str] = field(default_factory=list)
    incomplete_tables: set[str] = field(default_factory=set)

    def counts(self) -> dict[str, int]:
        return {
            "groups": len(self.groups),
            "projects": len(self.projects),
            "memberships": len(self.memberships),
            "users": len({item.user.external_id for item in self.memberships}),
            "work_items": len(self.work_items),
            "branches": len(self.branches),
            "iterations": len(self.iterations),
        }


class Collector(Protocol):
    source_name: str

    def check(self) -> str: ...

    def collect(self, date_from: date, date_to: date) -> list[TimeEntry]: ...


class CatalogCollector(Protocol):
    source_name: str

    def collect_catalog(self) -> CatalogSnapshot: ...
