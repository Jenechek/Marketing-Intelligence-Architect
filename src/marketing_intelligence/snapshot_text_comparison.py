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
    """Вернуть точное расстояние бит-параллельным алгоритмом Майерса."""

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

    character_masks: dict[str, int] = {}
    bit = 1
    for index in range(previous_start, previous_start + previous_length):
        character = previous[index]
        character_masks[character] = character_masks.get(character, 0) | bit
        bit <<= 1

    full_mask = (1 << previous_length) - 1
    highest_bit = 1 << (previous_length - 1)
    positive = full_mask
    negative = 0
    distance = previous_length

    for index in range(current_start, current_start + current_length):
        character_mask = character_masks.get(current[index], 0)
        matches = character_mask | negative
        differences = (((matches & positive) + positive) ^ positive) | matches
        horizontal_positive = negative | ~(differences | positive)
        horizontal_negative = differences & positive

        if horizontal_positive & highest_bit:
            distance += 1
        elif horizontal_negative & highest_bit:
            distance -= 1

        horizontal_positive = ((horizontal_positive << 1) | 1) & full_mask
        horizontal_negative = (horizontal_negative << 1) & full_mask
        positive = (
            horizontal_negative | ~(differences | horizontal_positive)
        ) & full_mask
        negative = horizontal_positive & differences

    return distance


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
