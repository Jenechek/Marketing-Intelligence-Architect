import tracemalloc
from dataclasses import FrozenInstanceError
from fractions import Fraction
from itertools import product

import pytest

from marketing_intelligence.snapshot_metadata_comparison import ChangeImportance
from marketing_intelligence.snapshot_text_comparison import (
    MatchedPageText,
    _levenshtein_distance,
    compare_matched_page_text,
)


def page(previous: str, current: str) -> MatchedPageText:
    return MatchedPageText(
        url="https://example.com/page",
        previous_page_id=10,
        current_page_id="20",
        previous_normalized_text=previous,
        current_normalized_text=current,
    )


def reference_levenshtein_distance(previous: str, current: str) -> int:
    """Простая построчная DP служит независимым эталоном только в тестах."""

    previous_row = list(range(len(previous) + 1))
    for current_index, current_character in enumerate(current, start=1):
        current_row = [current_index]
        for previous_index, previous_character in enumerate(previous, start=1):
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


def test_bit_parallel_core_matches_reference_dp_exhaustively() -> None:
    alphabet = "aя🦔"
    strings = [""]
    for length in range(1, 5):
        strings.extend(
            "".join(characters)
            for characters in product(alphabet, repeat=length)
        )

    for previous in strings:
        for current in strings:
            assert _levenshtein_distance(previous, current) == (
                reference_levenshtein_distance(previous, current)
            ), (previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [("", ""), ("одинаковый текст", "одинаковый текст")],
)
def test_equal_texts_do_not_create_change(previous: str, current: str) -> None:
    assert compare_matched_page_text(page(previous, current)) is None


@pytest.mark.parametrize(("previous", "current"), [("", "abc"), ("abc", "")])
def test_empty_and_nonempty_text_have_full_change(
    previous: str,
    current: str,
) -> None:
    result = compare_matched_page_text(page(previous, current))

    assert result is not None
    assert result.distance == 3
    assert result.change_ratio == 1
    assert result.importance is ChangeImportance.HIGH
    assert result.weight == 3


@pytest.mark.parametrize(
    ("previous", "current", "distance"),
    [
        ("кот", "крот", 1),
        ("крот", "кот", 1),
        ("кот", "кит", 1),
        ("ёжик 🦔", "ежик 🐾", 2),
    ],
)
def test_exact_distance_for_edits_and_unicode(
    previous: str,
    current: str,
    distance: int,
) -> None:
    result = compare_matched_page_text(page(previous, current))

    assert result is not None
    assert result.distance == distance
    assert result.change_ratio == Fraction(distance, max(len(previous), len(current)))


def test_text_is_not_normalized_again() -> None:
    result = compare_matched_page_text(page("Text", " text "))

    assert result is not None
    assert result.distance == 3
    assert result.change_ratio == Fraction(1, 2)


@pytest.mark.parametrize(
    ("previous", "current", "ratio", "importance", "weight"),
    [
        ("a" * 20, "b" + "a" * 19, Fraction(1, 20), ChangeImportance.LOW, 1),
        ("a" * 10, "b" + "a" * 9, Fraction(1, 10), ChangeImportance.MEDIUM, 2),
        ("a" * 10, "bbb" + "a" * 7, Fraction(3, 10), ChangeImportance.HIGH, 3),
    ],
)
def test_importance_uses_exact_boundaries(
    previous: str,
    current: str,
    ratio: Fraction,
    importance: ChangeImportance,
    weight: int,
) -> None:
    result = compare_matched_page_text(page(previous, current))

    assert result is not None
    assert result.change_ratio == ratio
    assert result.importance is importance
    assert result.weight == weight


@pytest.mark.parametrize(
    ("previous", "current"),
    [("kitten", "sitting"), ("данные", "длинные данные"), ("abc", "")],
)
def test_distance_and_ratio_are_symmetric(previous: str, current: str) -> None:
    forward = compare_matched_page_text(page(previous, current))
    backward = compare_matched_page_text(page(current, previous))

    assert forward is not None
    assert backward is not None
    assert forward.distance == backward.distance
    assert forward.change_ratio == backward.change_ratio


def test_result_contains_identity_and_is_immutable() -> None:
    result = compare_matched_page_text(page("старый", "новый"))

    assert result is not None
    assert result.url == "https://example.com/page"
    assert result.previous_page_id == 10
    assert result.current_page_id == "20"
    with pytest.raises(FrozenInstanceError):
        result.weight = 1  # type: ignore[misc]


def test_long_fully_different_texts_use_bounded_memory() -> None:
    matched_page = page("a" * 50_000, "b" * 50_000)

    tracemalloc.start()
    try:
        result = compare_matched_page_text(matched_page)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result is not None
    assert result.distance == 50_000
    assert peak_bytes < 150_000
