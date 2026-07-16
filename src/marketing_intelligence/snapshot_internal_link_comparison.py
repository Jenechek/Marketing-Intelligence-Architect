"""Сравнение множеств внутренних ссылок совпавшей пары страниц."""

from collections.abc import Collection
from dataclasses import dataclass
from fractions import Fraction

from .change_importance import ChangeImportance, classify_change_ratio


@dataclass(frozen=True)
class MatchedPageInternalLinks:
    """Уже нормализованные ссылки страниц, сопоставленных по URL."""

    url: str
    previous_page_id: int | str
    current_page_id: int | str
    previous_internal_links: Collection[str]
    current_internal_links: Collection[str]


@dataclass(frozen=True)
class InternalLinksChange:
    """Неизменяемый результат изменения множества внутренних ссылок."""

    url: str
    previous_page_id: int | str
    current_page_id: int | str
    added_links: tuple[str, ...]
    removed_links: tuple[str, ...]
    change_ratio: Fraction
    importance: ChangeImportance
    weight: int


def compare_matched_page_internal_links(
    page: MatchedPageInternalLinks,
) -> InternalLinksChange | None:
    """Сравнить ссылки как множества без повторной нормализации URL."""

    previous_links = set(page.previous_internal_links)
    current_links = set(page.current_internal_links)
    if previous_links == current_links:
        return None

    added_links = tuple(sorted(current_links - previous_links))
    removed_links = tuple(sorted(previous_links - current_links))
    change_ratio = Fraction(
        len(added_links) + len(removed_links),
        len(previous_links | current_links),
    )
    importance, weight = classify_change_ratio(change_ratio)
    return InternalLinksChange(
        url=page.url,
        previous_page_id=page.previous_page_id,
        current_page_id=page.current_page_id,
        added_links=added_links,
        removed_links=removed_links,
        change_ratio=change_ratio,
        importance=importance,
        weight=weight,
    )
