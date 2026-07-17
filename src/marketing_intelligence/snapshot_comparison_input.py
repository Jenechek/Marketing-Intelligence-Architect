"""Нейтральные неизменяемые данные полной пары снимков для сравнения."""

from dataclasses import dataclass
from datetime import datetime

from .snapshot_page_matching import SnapshotPageReference


@dataclass(frozen=True)
class SnapshotPageVersion:
    """Полное неизменяемое содержимое сохранённой версии страницы."""

    identifier: int
    url: str
    checked_at: datetime
    title: str | None
    description: str | None
    h1: str | None
    normalized_text: str
    content_hash: str
    internal_links: tuple[str, ...]


@dataclass(frozen=True)
class MatchedSnapshotPageVersions:
    """Предыдущая и текущая версии страницы с одинаковым URL."""

    previous: SnapshotPageVersion
    current: SnapshotPageVersion

    @property
    def url(self) -> str:
        return self.current.url


@dataclass(frozen=True)
class CompletedSnapshotComparisonInput:
    """Полная пара завершённых снимков для доменного сравнения."""

    current_run_id: int
    previous_run_id: int | None
    current_completed_at: datetime
    creates_baseline: bool
    new_pages: tuple[SnapshotPageReference, ...]
    removed_pages: tuple[SnapshotPageReference, ...]
    matched_pages: tuple[MatchedSnapshotPageVersions, ...]
