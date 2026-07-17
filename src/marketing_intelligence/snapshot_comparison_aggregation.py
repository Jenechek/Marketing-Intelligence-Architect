"""Чистая сборка единого результата сравнения пары снимков."""

from dataclasses import dataclass
from datetime import datetime

from .change_importance import ChangeImportance
from .snapshot_comparison_input import (
    CompletedSnapshotComparisonInput,
    MatchedSnapshotPageVersions,
    SnapshotPageVersion,
)
from .snapshot_internal_link_comparison import (
    InternalLinksChange,
    MatchedPageInternalLinks,
    compare_matched_page_internal_links,
)
from .snapshot_metadata_comparison import (
    MatchedPageMetadata,
    MetadataChange,
    compare_matched_page_metadata,
)
from .snapshot_page_matching import SnapshotPageReference
from .snapshot_text_comparison import (
    MatchedPageText,
    TextChange,
    compare_matched_page_text,
)


@dataclass(frozen=True)
class NewSnapshotPageComparison:
    """Новая страница, представленная только текущей версией."""

    current: SnapshotPageReference
    importance: ChangeImportance = ChangeImportance.MEDIUM
    weight: int = 2

    @property
    def url(self) -> str:
        return self.current.url


@dataclass(frozen=True)
class RemovedSnapshotPageComparison:
    """Удалённая страница, представленная только предыдущей версией."""

    previous: SnapshotPageReference
    importance: ChangeImportance = ChangeImportance.HIGH
    weight: int = 3

    @property
    def url(self) -> str:
        return self.previous.url


@dataclass(frozen=True)
class ChangedSnapshotPageComparison:
    """Все изменения совпавшей страницы с обеими полными версиями."""

    current: SnapshotPageVersion
    previous: SnapshotPageVersion
    metadata_changes: tuple[MetadataChange, ...]
    text_change: TextChange | None
    internal_links_change: InternalLinksChange | None
    importance: ChangeImportance
    weight: int

    @property
    def url(self) -> str:
        return self.current.url


@dataclass(frozen=True)
class CompletedSnapshotComparisonResult:
    """Неизменяемый полный результат сравнения завершённой пары снимков."""

    current_run_id: int
    previous_run_id: int | None
    current_completed_at: datetime
    creates_baseline: bool
    new_pages: tuple[NewSnapshotPageComparison, ...]
    removed_pages: tuple[RemovedSnapshotPageComparison, ...]
    changed_pages: tuple[ChangedSnapshotPageComparison, ...]


def build_completed_snapshot_comparison(
    comparison_input: CompletedSnapshotComparisonInput,
) -> CompletedSnapshotComparisonResult:
    """Собрать полный результат, не обращаясь к слою хранения."""

    if comparison_input.creates_baseline:
        return CompletedSnapshotComparisonResult(
            current_run_id=comparison_input.current_run_id,
            previous_run_id=comparison_input.previous_run_id,
            current_completed_at=comparison_input.current_completed_at,
            creates_baseline=True,
            new_pages=(),
            removed_pages=(),
            changed_pages=(),
        )

    changed_pages = tuple(
        page
        for matched in sorted(comparison_input.matched_pages, key=lambda item: item.url)
        if (page := _compare_matched_page(matched)) is not None
    )
    return CompletedSnapshotComparisonResult(
        current_run_id=comparison_input.current_run_id,
        previous_run_id=comparison_input.previous_run_id,
        current_completed_at=comparison_input.current_completed_at,
        creates_baseline=False,
        new_pages=tuple(
            NewSnapshotPageComparison(current=page)
            for page in sorted(comparison_input.new_pages, key=lambda item: item.url)
        ),
        removed_pages=tuple(
            RemovedSnapshotPageComparison(previous=page)
            for page in sorted(comparison_input.removed_pages, key=lambda item: item.url)
        ),
        changed_pages=changed_pages,
    )


def _compare_matched_page(
    matched: MatchedSnapshotPageVersions,
) -> ChangedSnapshotPageComparison | None:
    previous = matched.previous
    current = matched.current
    identity = {
        "url": current.url,
        "previous_page_id": previous.identifier,
        "current_page_id": current.identifier,
    }
    metadata_changes = compare_matched_page_metadata(
        MatchedPageMetadata(
            **identity,
            previous_title=previous.title,
            current_title=current.title,
            previous_description=previous.description,
            current_description=current.description,
            previous_h1=previous.h1,
            current_h1=current.h1,
        )
    )
    text_change = compare_matched_page_text(
        MatchedPageText(
            **identity,
            previous_normalized_text=previous.normalized_text,
            current_normalized_text=current.normalized_text,
        )
    )
    internal_links_change = compare_matched_page_internal_links(
        MatchedPageInternalLinks(
            **identity,
            previous_internal_links=previous.internal_links,
            current_internal_links=current.internal_links,
        )
    )
    weights = [change.weight for change in metadata_changes]
    weights.extend(
        change.weight
        for change in (text_change, internal_links_change)
        if change is not None
    )
    if not weights:
        return None
    weight = max(weights)
    return ChangedSnapshotPageComparison(
        current=current,
        previous=previous,
        metadata_changes=metadata_changes,
        text_change=text_change,
        internal_links_change=internal_links_change,
        importance=_importance_for_weight(weight),
        weight=weight,
    )


def _importance_for_weight(weight: int) -> ChangeImportance:
    return {
        1: ChangeImportance.LOW,
        2: ChangeImportance.MEDIUM,
        3: ChangeImportance.HIGH,
    }[weight]


aggregate_completed_snapshot_comparison = build_completed_snapshot_comparison
