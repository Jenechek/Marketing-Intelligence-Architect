from dataclasses import FrozenInstanceError

import pytest

from marketing_intelligence.snapshot_page_matching import (
    DuplicateSnapshotPageUrlError,
    SnapshotPageReference,
    match_snapshot_pages,
)


def page(identifier: int, url: str) -> SnapshotPageReference:
    return SnapshotPageReference(identifier=identifier, url=url)


def urls(values: tuple[SnapshotPageReference, ...]) -> tuple[str, ...]:
    return tuple(value.url for value in values)


def test_first_snapshot_creates_baseline_without_new_pages() -> None:
    result = match_snapshot_pages(
        [page(2, "https://example.com/b"), page(1, "https://example.com/a")]
    )

    assert result.creates_baseline is True
    assert urls(result.baseline_pages) == (
        "https://example.com/a",
        "https://example.com/b",
    )
    assert result.current_only == ()
    assert result.previous_only == ()
    assert result.matched == ()


def test_identical_snapshots_match_every_page() -> None:
    result = match_snapshot_pages(
        [page(12, "https://example.com/b"), page(11, "https://example.com/a")],
        [page(2, "https://example.com/b"), page(1, "https://example.com/a")],
    )

    assert result.creates_baseline is False
    assert result.baseline_pages == ()
    assert result.current_only == ()
    assert result.previous_only == ()
    assert tuple(match.url for match in result.matched) == (
        "https://example.com/a",
        "https://example.com/b",
    )
    assert tuple(
        (match.current.identifier, match.previous.identifier)
        for match in result.matched
    ) == ((11, 1), (12, 2))


def test_result_contains_current_only_previous_only_and_matched_pages() -> None:
    result = match_snapshot_pages(
        [page(12, "https://example.com/same"), page(13, "https://example.com/new")],
        [page(2, "https://example.com/same"), page(3, "https://example.com/removed")],
    )

    assert urls(result.current_only) == ("https://example.com/new",)
    assert urls(result.previous_only) == ("https://example.com/removed",)
    assert tuple(match.url for match in result.matched) == (
        "https://example.com/same",
    )


def test_result_order_is_stable_and_independent_of_input_order() -> None:
    current = [
        page(13, "https://example.com/z-new"),
        page(12, "https://example.com/matched-b"),
        page(11, "https://example.com/matched-a"),
        page(14, "https://example.com/a-new"),
    ]
    previous = [
        page(4, "https://example.com/z-removed"),
        page(2, "https://example.com/matched-b"),
        page(1, "https://example.com/matched-a"),
        page(3, "https://example.com/a-removed"),
    ]

    first = match_snapshot_pages(current, previous)
    second = match_snapshot_pages(list(reversed(current)), list(reversed(previous)))

    assert first == second
    assert urls(first.current_only) == (
        "https://example.com/a-new",
        "https://example.com/z-new",
    )
    assert urls(first.previous_only) == (
        "https://example.com/a-removed",
        "https://example.com/z-removed",
    )
    assert tuple(match.url for match in first.matched) == (
        "https://example.com/matched-a",
        "https://example.com/matched-b",
    )


@pytest.mark.parametrize(
    ("current", "previous", "collection_name"),
    [
        (
            [page(1, "https://example.com/a"), page(2, "https://example.com/a")],
            None,
            "текущего снимка",
        ),
        (
            [page(1, "https://example.com/a")],
            [page(2, "https://example.com/b"), page(3, "https://example.com/b")],
            "предыдущего снимка",
        ),
    ],
)
def test_duplicate_url_is_rejected_with_domain_error(
    current: list[SnapshotPageReference],
    previous: list[SnapshotPageReference] | None,
    collection_name: str,
) -> None:
    with pytest.raises(DuplicateSnapshotPageUrlError) as error:
        match_snapshot_pages(current, previous)

    assert error.value.collection_name == collection_name
    assert error.value.url in str(error.value)
    assert "повторяющийся URL" in str(error.value)


def test_input_values_and_result_are_immutable() -> None:
    reference = page(1, "https://example.com/a")
    result = match_snapshot_pages([reference])

    with pytest.raises(FrozenInstanceError):
        reference.url = "https://example.com/changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.creates_baseline = False  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.baseline_pages.append(reference)  # type: ignore[attr-defined]


def test_urls_are_compared_exactly_without_normalization() -> None:
    result = match_snapshot_pages(
        [page(2, "https://EXAMPLE.com/path")],
        [page(1, "https://example.com/path")],
    )

    assert urls(result.current_only) == ("https://EXAMPLE.com/path",)
    assert urls(result.previous_only) == ("https://example.com/path",)
    assert result.matched == ()
