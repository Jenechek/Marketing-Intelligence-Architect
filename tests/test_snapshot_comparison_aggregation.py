import ast
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from marketing_intelligence.change_importance import ChangeImportance
from marketing_intelligence.snapshot_comparison_aggregation import (
    build_completed_snapshot_comparison,
)
from marketing_intelligence.snapshot_comparison_input import (
    CompletedSnapshotComparisonInput,
    MatchedSnapshotPageVersions,
    SnapshotPageVersion,
)
from marketing_intelligence.snapshot_metadata_comparison import MetadataField
from marketing_intelligence.snapshot_page_matching import SnapshotPageReference
from marketing_intelligence.snapshot_pair_storage import (
    CompletedSnapshotComparisonInput as StorageComparisonInput,
)


COMPLETED_AT = datetime(2026, 7, 17, 10, tzinfo=UTC)


def version(identifier: int, url: str, **changes) -> SnapshotPageVersion:
    values = {
        "checked_at": COMPLETED_AT,
        "title": "Title",
        "description": "Description",
        "h1": "H1",
        "normalized_text": "a" * 100,
        "content_hash": f"hash-{identifier}",
        "internal_links": ("/shared",),
    }
    values.update(changes)
    return SnapshotPageVersion(identifier=identifier, url=url, **values)


def comparison_input(
    *,
    creates_baseline: bool = False,
    new_pages=(),
    removed_pages=(),
    matched_pages=(),
) -> CompletedSnapshotComparisonInput:
    return CompletedSnapshotComparisonInput(
        current_run_id=20,
        previous_run_id=None if creates_baseline else 10,
        current_completed_at=COMPLETED_AT,
        creates_baseline=creates_baseline,
        new_pages=tuple(new_pages),
        removed_pages=tuple(removed_pages),
        matched_pages=tuple(matched_pages),
    )


def matched(url: str = "https://example.com/page", **current_changes):
    return MatchedSnapshotPageVersions(
        previous=version(10, url),
        current=version(20, url, **current_changes),
    )


def test_first_baseline_always_has_empty_change_collections() -> None:
    result = build_completed_snapshot_comparison(
        comparison_input(
            creates_baseline=True,
            new_pages=(SnapshotPageReference(20, "https://example.com/new"),),
            matched_pages=(matched(title="Changed"),),
        )
    )

    assert (
        result.current_run_id,
        result.previous_run_id,
        result.current_completed_at,
        result.creates_baseline,
    ) == (20, None, COMPLETED_AT, True)
    assert result.new_pages == result.removed_pages == result.changed_pages == ()


def test_new_and_removed_pages_have_fixed_importance_and_explicit_side() -> None:
    new = SnapshotPageReference(20, "https://example.com/new")
    removed = SnapshotPageReference(10, "https://example.com/removed")
    result = build_completed_snapshot_comparison(
        comparison_input(new_pages=(new,), removed_pages=(removed,))
    )

    assert result.new_pages[0].current is new
    assert (result.new_pages[0].importance, result.new_pages[0].weight) == (
        ChangeImportance.MEDIUM,
        2,
    )
    assert result.removed_pages[0].previous is removed
    assert (result.removed_pages[0].importance, result.removed_pages[0].weight) == (
        ChangeImportance.HIGH,
        3,
    )


def test_fully_unchanged_matched_page_is_omitted() -> None:
    result = build_completed_snapshot_comparison(
        comparison_input(matched_pages=(matched(),))
    )

    assert result.changed_pages == ()


def test_metadata_changes_keep_values_and_title_description_h1_order() -> None:
    result = build_completed_snapshot_comparison(
        comparison_input(
            matched_pages=(
                matched(title=None, description="", h1="New H1"),
            )
        )
    )
    page = result.changed_pages[0]

    assert tuple(change.field for change in page.metadata_changes) == (
        MetadataField.TITLE,
        MetadataField.DESCRIPTION,
        MetadataField.H1,
    )
    assert tuple(change.previous_value for change in page.metadata_changes) == (
        "Title",
        "Description",
        "H1",
    )
    assert tuple(change.current_value for change in page.metadata_changes) == (
        None,
        "",
        "New H1",
    )
    assert (page.importance, page.weight) == (ChangeImportance.HIGH, 3)


@pytest.mark.parametrize(
    ("current_changes", "importance", "weight"),
    [
        ({"normalized_text": "b" + "a" * 99}, ChangeImportance.LOW, 1),
        (
            {"normalized_text": "b" * 10 + "a" * 90},
            ChangeImportance.MEDIUM,
            2,
        ),
        ({"normalized_text": ""}, ChangeImportance.HIGH, 3),
    ],
)
def test_text_and_links_cover_low_medium_and_high(
    current_changes, importance, weight
) -> None:
    result = build_completed_snapshot_comparison(
        comparison_input(matched_pages=(matched(**current_changes),))
    )

    assert (result.changed_pages[0].importance, result.changed_pages[0].weight) == (
        importance,
        weight,
    )


def test_internal_links_change_is_aggregated_independently() -> None:
    page = build_completed_snapshot_comparison(
        comparison_input(
            matched_pages=(matched(internal_links=("/changed",)),)
        )
    ).changed_pages[0]

    assert page.metadata_changes == ()
    assert page.text_change is None
    assert page.internal_links_change is not None
    assert page.internal_links_change.added_links == ("/changed",)
    assert page.internal_links_change.removed_links == ("/shared",)
    assert (page.importance, page.weight) == (ChangeImportance.HIGH, 3)


def test_combined_changes_use_maximum_weight_and_keep_both_full_versions() -> None:
    pair = matched(
        title="New title",
        normalized_text="b" + "a" * 99,
        internal_links=("/shared", "/added"),
    )
    page = build_completed_snapshot_comparison(
        comparison_input(matched_pages=(pair,))
    ).changed_pages[0]

    assert page.current is pair.current
    assert page.previous is pair.previous
    assert page.current.normalized_text == "b" + "a" * 99
    assert page.previous.normalized_text == "a" * 100
    assert page.metadata_changes and page.text_change and page.internal_links_change
    assert (page.importance, page.weight) == (ChangeImportance.HIGH, 3)


def test_none_and_empty_string_remain_unambiguous_current_and_previous() -> None:
    pair = MatchedSnapshotPageVersions(
        previous=version(10, "https://example.com/page", title=None),
        current=version(20, "https://example.com/page", title=""),
    )
    page = build_completed_snapshot_comparison(
        comparison_input(matched_pages=(pair,))
    ).changed_pages[0]

    assert page.current.title == ""
    assert page.previous.title is None
    assert page.metadata_changes[0].current_value == ""
    assert page.metadata_changes[0].previous_value is None


def test_every_page_collection_is_sorted_by_url() -> None:
    urls = ("https://example.com/z", "https://example.com/a", "https://example.com/я")
    result = build_completed_snapshot_comparison(
        comparison_input(
            new_pages=tuple(SnapshotPageReference(i, url) for i, url in enumerate(urls)),
            removed_pages=tuple(
                SnapshotPageReference(i + 10, url) for i, url in enumerate(reversed(urls))
            ),
            matched_pages=tuple(matched(url, title="Changed") for url in reversed(urls)),
        )
    )

    expected = tuple(sorted(urls))
    assert tuple(page.url for page in result.new_pages) == expected
    assert tuple(page.url for page in result.removed_pages) == expected
    assert tuple(page.url for page in result.changed_pages) == expected


def test_results_and_input_types_are_immutable_and_storage_import_is_compatible() -> None:
    pair = matched(title="Changed")
    source = comparison_input(matched_pages=(pair,))
    result = build_completed_snapshot_comparison(source)

    assert StorageComparisonInput is CompletedSnapshotComparisonInput
    with pytest.raises(FrozenInstanceError):
        result.current_run_id = 30  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.changed_pages[0].current = replace(pair.current)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.changed_pages.append(result.changed_pages[0])  # type: ignore[attr-defined]


def test_domain_aggregation_has_no_storage_framework_imports() -> None:
    module_path = __import__(
        "marketing_intelligence.snapshot_comparison_aggregation",
        fromlist=["__file__"],
    ).__file__
    assert module_path is not None
    with open(module_path, encoding="utf-8") as module_file:
        tree = ast.parse(module_file.read())
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint({"sqlmodel", "sqlalchemy", "sqlite3"})
