from dataclasses import FrozenInstanceError, replace

import pytest

from marketing_intelligence.snapshot_metadata_comparison import (
    ChangeImportance,
    MatchedPageMetadata,
    MetadataField,
    compare_matched_page_metadata,
)


def page(**changes: str | None) -> MatchedPageMetadata:
    values = {
        "url": "https://example.com/page",
        "previous_page_id": 10,
        "current_page_id": 20,
        "previous_title": "Title",
        "current_title": "Title",
        "previous_description": "Description",
        "current_description": "Description",
        "previous_h1": "H1",
        "current_h1": "H1",
    }
    values.update(changes)
    return MatchedPageMetadata(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current_name", "field", "importance", "weight"),
    [
        ("current_title", MetadataField.TITLE, ChangeImportance.HIGH, 3),
        (
            "current_description",
            MetadataField.DESCRIPTION,
            ChangeImportance.MEDIUM,
            2,
        ),
        ("current_h1", MetadataField.H1, ChangeImportance.HIGH, 3),
    ],
)
def test_each_metadata_field_creates_its_own_change(
    current_name: str,
    field: MetadataField,
    importance: ChangeImportance,
    weight: int,
) -> None:
    result = compare_matched_page_metadata(page(**{current_name: "Changed"}))

    assert len(result) == 1
    assert result[0].url == "https://example.com/page"
    assert result[0].previous_page_id == 10
    assert result[0].current_page_id == 20
    assert result[0].field is field
    assert result[0].current_value == "Changed"
    assert result[0].importance is importance
    assert result[0].weight == weight


def test_all_changes_are_returned_in_stable_field_order() -> None:
    result = compare_matched_page_metadata(
        page(
            current_title="New title",
            current_description="New description",
            current_h1="New H1",
        )
    )

    assert tuple(change.field for change in result) == (
        MetadataField.TITLE,
        MetadataField.DESCRIPTION,
        MetadataField.H1,
    )
    assert tuple(change.previous_value for change in result) == (
        "Title",
        "Description",
        "H1",
    )
    assert tuple(change.current_value for change in result) == (
        "New title",
        "New description",
        "New H1",
    )


def test_unchanged_fields_do_not_create_results() -> None:
    assert compare_matched_page_metadata(page()) == ()


@pytest.mark.parametrize(
    ("previous_value", "current_value"),
    [(None, ""), ("", None)],
)
def test_none_and_empty_string_are_different(
    previous_value: str | None,
    current_value: str | None,
) -> None:
    result = compare_matched_page_metadata(
        page(previous_title=previous_value, current_title=current_value)
    )

    assert len(result) == 1
    assert result[0].previous_value is previous_value
    assert result[0].current_value is current_value


@pytest.mark.parametrize(
    ("previous_value", "current_value"),
    [("Title", " title"), ("Title", "title")],
)
def test_values_are_compared_exactly_without_normalization(
    previous_value: str,
    current_value: str,
) -> None:
    result = compare_matched_page_metadata(
        page(previous_title=previous_value, current_title=current_value)
    )

    assert len(result) == 1
    assert result[0].previous_value == previous_value
    assert result[0].current_value == current_value


def test_input_and_change_results_are_immutable() -> None:
    matched_page = page(current_title="Changed")
    result = compare_matched_page_metadata(matched_page)

    with pytest.raises(FrozenInstanceError):
        matched_page.url = "https://example.com/other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result[0].weight = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.append(result[0])  # type: ignore[attr-defined]


def test_result_is_deterministic_for_the_same_input() -> None:
    matched_page = page(
        current_title="New title",
        current_description="New description",
        current_h1="New H1",
    )

    assert compare_matched_page_metadata(matched_page) == (
        compare_matched_page_metadata(replace(matched_page))
    )
