from dataclasses import FrozenInstanceError
from fractions import Fraction

import pytest

from marketing_intelligence.snapshot_metadata_comparison import ChangeImportance
from marketing_intelligence.snapshot_internal_link_comparison import (
    MatchedPageInternalLinks,
    compare_matched_page_internal_links,
)


def page(
    previous: list[str] | tuple[str, ...],
    current: list[str] | tuple[str, ...],
) -> MatchedPageInternalLinks:
    return MatchedPageInternalLinks(
        url="https://example.com/page",
        previous_page_id=10,
        current_page_id="20",
        previous_internal_links=previous,
        current_internal_links=current,
    )


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (["/a", "/b"], ["/b", "/a"]),
        (["/a", "/a", "/b"], ["/a", "/b", "/b"]),
        ([], []),
    ],
)
def test_equal_sets_do_not_create_change(
    previous: list[str], current: list[str]
) -> None:
    assert compare_matched_page_internal_links(page(previous, current)) is None


@pytest.mark.parametrize(
    ("previous", "current", "added", "removed"),
    [
        ([], ["/a", "/b"], ("/a", "/b"), ()),
        (["/a", "/b"], [], (), ("/a", "/b")),
    ],
)
def test_empty_and_nonempty_sets_have_full_change(
    previous: list[str],
    current: list[str],
    added: tuple[str, ...],
    removed: tuple[str, ...],
) -> None:
    result = compare_matched_page_internal_links(page(previous, current))

    assert result is not None
    assert result.added_links == added
    assert result.removed_links == removed
    assert result.change_ratio == Fraction(1, 1)
    assert result.importance is ChangeImportance.HIGH
    assert result.weight == 3


def test_additions_and_removals_are_separate_and_stably_sorted() -> None:
    result = compare_matched_page_internal_links(
        page(["/z", "/same", "/b"], ["/я", "/a", "/same"])
    )

    assert result is not None
    assert result.added_links == ("/a", "/я")
    assert result.removed_links == ("/b", "/z")
    assert result.change_ratio == Fraction(4, 5)


def test_urls_are_compared_exactly_without_normalization() -> None:
    result = compare_matched_page_internal_links(
        page(
            ["https://example.com/Путь", "https://example.com/%D0%9F%D1%83%D1%82%D1%8C"],
            ["https://example.com/путь", "https://example.com/Путь/"],
        )
    )

    assert result is not None
    assert result.added_links == (
        "https://example.com/Путь/",
        "https://example.com/путь",
    )
    assert result.removed_links == (
        "https://example.com/%D0%9F%D1%83%D1%82%D1%8C",
        "https://example.com/Путь",
    )


def test_ratio_is_symmetric_and_sides_exchange_added_and_removed() -> None:
    previous = ["/a", "/b", "/same"]
    current = ["/c", "/d", "/same"]

    forward = compare_matched_page_internal_links(page(previous, current))
    backward = compare_matched_page_internal_links(page(current, previous))

    assert forward is not None
    assert backward is not None
    assert forward.change_ratio == backward.change_ratio == Fraction(4, 5)
    assert forward.added_links == backward.removed_links
    assert forward.removed_links == backward.added_links


@pytest.mark.parametrize(
    ("shared_count", "changed_count", "ratio", "importance", "weight"),
    [
        (99, 1, Fraction(1, 100), ChangeImportance.LOW, 1),
        (9, 1, Fraction(1, 10), ChangeImportance.MEDIUM, 2),
        (7, 3, Fraction(3, 10), ChangeImportance.HIGH, 3),
    ],
)
def test_importance_uses_exact_boundaries(
    shared_count: int,
    changed_count: int,
    ratio: Fraction,
    importance: ChangeImportance,
    weight: int,
) -> None:
    shared = [f"/shared/{index}" for index in range(shared_count)]
    added = [f"/added/{index}" for index in range(changed_count)]
    result = compare_matched_page_internal_links(page(shared, shared + added))

    assert result is not None
    assert result.change_ratio == ratio
    assert result.importance is importance
    assert result.weight == weight


def test_result_contains_identity_and_is_immutable() -> None:
    result = compare_matched_page_internal_links(page(["/old"], ["/new"]))

    assert result is not None
    assert result.url == "https://example.com/page"
    assert result.previous_page_id == 10
    assert result.current_page_id == "20"
    with pytest.raises(FrozenInstanceError):
        result.weight = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.added_links.append("/other")  # type: ignore[attr-defined]


def test_large_collections_are_compared_as_sets() -> None:
    previous = [f"https://example.com/{index:06d}" for index in range(100_000)]
    current = list(reversed(previous))
    current[-1] = "https://example.com/new"

    result = compare_matched_page_internal_links(page(previous, current))

    assert result is not None
    assert result.added_links == ("https://example.com/new",)
    assert result.removed_links == ("https://example.com/000000",)
    assert result.change_ratio == Fraction(2, 100_001)
