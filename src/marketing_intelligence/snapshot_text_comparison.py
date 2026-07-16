"""Точное сравнение нормализованного текста совпавшей пары страниц."""

from dataclasses import dataclass
from fractions import Fraction

from marketing_intelligence.snapshot_metadata_comparison import ChangeImportance


@dataclass(frozen=True)
class MatchedPageText:
    """Уже нормализованные тексты страниц, сопоставленных по URL."""

    url: str
    previous_page_id: int | str
    current_page_id: int | str
    previous_normalized_text: str
    current_normalized_text: str


@dataclass(frozen=True)
class TextChange:
    """Одно неизменяемое изменение нормализованного текста страницы."""

    url: str
    previous_page_id: int | str
    current_page_id: int | str
    distance: int
    change_ratio: Fraction
    importance: ChangeImportance
    weight: int


def _levenshtein_distance(previous: str, current: str) -> int:
    """Вернуть точное расстояние, используя память по меньшей строке."""

    if previous == current:
        return 0
    if not previous:
        return len(current)
    if not current:
        return len(previous)

    prefix_length = 0
    shared_limit = min(len(previous), len(current))
    while (
        prefix_length < shared_limit
        and previous[prefix_length] == current[prefix_length]
    ):
        prefix_length += 1

    previous_end = len(previous)
    current_end = len(current)
    while (
        previous_end > prefix_length
        and current_end > prefix_length
        and previous[previous_end - 1] == current[current_end - 1]
    ):
        previous_end -= 1
        current_end -= 1

    previous_length = previous_end - prefix_length
    current_length = current_end - prefix_length
    if not previous_length:
        return current_length
    if not current_length:
        return previous_length

    previous_start = current_start = prefix_length
    if previous_length > current_length:
        previous, current = current, previous
        previous_start, current_start = current_start, previous_start
        previous_length, current_length = current_length, previous_length

    previous_row = list(range(previous_length + 1))
    for current_index in range(1, current_length + 1):
        current_character = current[current_start + current_index - 1]
        current_row = [current_index]
        for previous_index in range(1, previous_length + 1):
            previous_character = previous[previous_start + previous_index - 1]
            current_row.append(
                min(
                    current_row[-1] + 1,
                    previous_row[previous_index] + 1,
                    previous_row[previous_index - 1]
                    + (previous_character != current_character),
                )
            )
        previous_row = current_row
    return previous_row[-1]


def _classify_change(change_ratio: Fraction) -> tuple[ChangeImportance, int]:
    if change_ratio < Fraction(1, 10):
        return ChangeImportance.LOW, 1
    if change_ratio < Fraction(3, 10):
        return ChangeImportance.MEDIUM, 2
    return ChangeImportance.HIGH, 3


def compare_matched_page_text(page: MatchedPageText) -> TextChange | None:
    """Вернуть точное изменение уже нормализованного текста или ``None``."""

    if page.previous_normalized_text == page.current_normalized_text:
        return None

    distance = _levenshtein_distance(
        page.previous_normalized_text,
        page.current_normalized_text,
    )
    change_ratio = Fraction(
        distance,
        max(
            len(page.previous_normalized_text),
            len(page.current_normalized_text),
        ),
    )
    importance, weight = _classify_change(change_ratio)
    return TextChange(
        url=page.url,
        previous_page_id=page.previous_page_id,
        current_page_id=page.current_page_id,
        distance=distance,
        change_ratio=change_ratio,
        importance=importance,
        weight=weight,
    )
