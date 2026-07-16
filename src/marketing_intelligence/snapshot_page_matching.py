"""Детерминированное сопоставление страниц двух снимков по URL."""

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotPageReference:
    """Неизменяемая ссылка на страницу снимка с уже нормализованным URL."""

    identifier: int | str
    url: str


@dataclass(frozen=True)
class MatchedSnapshotPages:
    """Страницы текущего и предыдущего снимков с одинаковым URL."""

    current: SnapshotPageReference
    previous: SnapshotPageReference

    @property
    def url(self) -> str:
        return self.current.url


@dataclass(frozen=True)
class SnapshotPageMatchResult:
    """Неизменяемый результат сопоставления страниц двух снимков."""

    creates_baseline: bool
    baseline_pages: tuple[SnapshotPageReference, ...]
    current_only: tuple[SnapshotPageReference, ...]
    previous_only: tuple[SnapshotPageReference, ...]
    matched: tuple[MatchedSnapshotPages, ...]


@dataclass(frozen=True)
class CompletedSnapshotPair:
    """Результат сопоставления выбранного завершённого запуска с предыдущим."""

    current_run_id: int
    previous_run_id: int | None
    match_result: SnapshotPageMatchResult


class DuplicateSnapshotPageUrlError(ValueError):
    """В одной коллекции снимка повторяется URL страницы."""

    def __init__(self, collection_name: str, url: str) -> None:
        self.collection_name = collection_name
        self.url = url
        super().__init__(
            f"Коллекция {collection_name} содержит повторяющийся URL страницы: {url}"
        )


def match_snapshot_pages(
    current: Collection[SnapshotPageReference],
    previous: Collection[SnapshotPageReference] | None = None,
) -> SnapshotPageMatchResult:
    """Сопоставить страницы по точному URL без повторной нормализации."""

    current_by_url = _index_by_url(current, "текущего снимка")
    current_urls = set(current_by_url)

    if previous is None:
        return SnapshotPageMatchResult(
            creates_baseline=True,
            baseline_pages=_ordered_pages(current_by_url, current_urls),
            current_only=(),
            previous_only=(),
            matched=(),
        )

    previous_by_url = _index_by_url(previous, "предыдущего снимка")
    previous_urls = set(previous_by_url)
    common_urls = current_urls & previous_urls

    return SnapshotPageMatchResult(
        creates_baseline=False,
        baseline_pages=(),
        current_only=_ordered_pages(current_by_url, current_urls - previous_urls),
        previous_only=_ordered_pages(previous_by_url, previous_urls - current_urls),
        matched=tuple(
            MatchedSnapshotPages(
                current=current_by_url[url],
                previous=previous_by_url[url],
            )
            for url in sorted(common_urls)
        ),
    )


def _index_by_url(
    pages: Collection[SnapshotPageReference],
    collection_name: str,
) -> dict[str, SnapshotPageReference]:
    indexed: dict[str, SnapshotPageReference] = {}
    for page in pages:
        if page.url in indexed:
            raise DuplicateSnapshotPageUrlError(collection_name, page.url)
        indexed[page.url] = page
    return indexed


def _ordered_pages(
    pages_by_url: dict[str, SnapshotPageReference],
    urls: set[str],
) -> tuple[SnapshotPageReference, ...]:
    return tuple(pages_by_url[url] for url in sorted(urls))
