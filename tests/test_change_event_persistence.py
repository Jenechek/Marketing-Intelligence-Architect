from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from marketing_intelligence.change_event_persistence import (
    ChangeEventType,
    save_snapshot_comparison_events,
)
from marketing_intelligence.change_importance import ChangeImportance
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.models import (
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    Site,
    SnapshotChangeEvent,
)
from marketing_intelligence.sites import delete_site
from marketing_intelligence.snapshot_comparison_aggregation import (
    CompletedSnapshotComparisonResult,
    NewSnapshotPageComparison,
    build_completed_snapshot_comparison,
)
from marketing_intelligence.snapshot_comparison_input import (
    CompletedSnapshotComparisonInput,
    MatchedSnapshotPageVersions,
    SnapshotPageVersion,
)
from marketing_intelligence.snapshot_page_matching import SnapshotPageReference


LOCAL_COMPLETED_AT = datetime(
    2026,
    7,
    17,
    13,
    tzinfo=timezone(timedelta(hours=3)),
)
UTC_COMPLETED_AT = datetime(2026, 7, 17, 10, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path):
    result = build_engine(f"sqlite:///{(tmp_path / 'events.db').as_posix()}")
    initialize_database(result)
    yield result
    result.dispose()


def add_run(session: Session, site_id: int, completed_at: datetime) -> int:
    run = CrawlRun(
        site_id=site_id,
        status="completed",
        message="ok",
        completed_at=completed_at,
        max_pages=10,
        max_depth=2,
        delay=0,
        timeout=5,
        user_agent="test",
    )
    session.add(run)
    session.flush()
    assert run.id is not None
    return run.id


def add_page(
    session: Session,
    run_id: int,
    sequence_number: int,
    url: str,
    *,
    title: str | None = "Title",
    description: str | None = "Description",
    h1: str | None = "H1",
    normalized_text: str = "a" * 10,
    internal_links_json: str = '["/shared","/old"]',
) -> int:
    record = CrawlPageRecord(
        crawl_run_id=run_id,
        sequence_number=sequence_number,
        url=url,
        depth=0,
        outcome="html",
        message="ok",
    )
    session.add(record)
    session.flush()
    assert record.id is not None
    session.add(
        CrawlPageSnapshot(
            crawl_page_record_id=record.id,
            checked_at=UTC_COMPLETED_AT,
            title=title,
            description=description,
            h1=h1,
            normalized_text=normalized_text,
            content_hash=f"hash-{record.id}",
            internal_links_json=internal_links_json,
        )
    )
    return record.id


def page_version(
    identifier: int,
    url: str,
    *,
    title: str | None = "Title",
    description: str | None = "Description",
    h1: str | None = "H1",
    normalized_text: str = "a" * 10,
    internal_links: tuple[str, ...] = ("/shared", "/old"),
) -> SnapshotPageVersion:
    return SnapshotPageVersion(
        identifier=identifier,
        url=url,
        checked_at=UTC_COMPLETED_AT,
        title=title,
        description=description,
        h1=h1,
        normalized_text=normalized_text,
        content_hash=f"hash-{identifier}",
        internal_links=internal_links,
    )


def create_mixed_result(engine) -> CompletedSnapshotComparisonResult:
    with Session(engine) as session:
        site = Site(name="Сайт", url="https://example.com")
        session.add(site)
        session.flush()
        assert site.id is not None
        previous_run_id = add_run(
            session,
            site.id,
            UTC_COMPLETED_AT - timedelta(hours=1),
        )
        removed_id = add_page(
            session,
            previous_run_id,
            1,
            "https://example.com/removed",
        )
        previous_id = add_page(
            session,
            previous_run_id,
            2,
            "https://example.com/same",
        )
        current_run_id = add_run(session, site.id, UTC_COMPLETED_AT)
        added_id = add_page(
            session,
            current_run_id,
            1,
            "https://example.com/added",
        )
        current_id = add_page(
            session,
            current_run_id,
            2,
            "https://example.com/same",
            title="New title",
            description="New description",
            h1="New H1",
            normalized_text="bbb" + "a" * 7,
            internal_links_json='["/shared","/new"]',
        )
        session.commit()

    comparison_input = CompletedSnapshotComparisonInput(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        current_completed_at=LOCAL_COMPLETED_AT,
        creates_baseline=False,
        new_pages=(
            SnapshotPageReference(
                identifier=added_id,
                url="https://example.com/added",
            ),
        ),
        removed_pages=(
            SnapshotPageReference(
                identifier=removed_id,
                url="https://example.com/removed",
            ),
        ),
        matched_pages=(
            MatchedSnapshotPageVersions(
                previous=page_version(
                    previous_id,
                    "https://example.com/same",
                ),
                current=page_version(
                    current_id,
                    "https://example.com/same",
                    title="New title",
                    description="New description",
                    h1="New H1",
                    normalized_text="bbb" + "a" * 7,
                    internal_links=("/shared", "/new"),
                ),
            ),
        ),
    )
    return build_completed_snapshot_comparison(comparison_input)


def load_events(engine) -> list[SnapshotChangeEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(SnapshotChangeEvent).order_by(
                    SnapshotChangeEvent.event_type,
                    SnapshotChangeEvent.url,
                )
            ).all()
        )


def test_mixed_result_creates_exact_separate_events_with_links_weights_and_utc(
    engine,
) -> None:
    result = create_mixed_result(engine)

    assert save_snapshot_comparison_events(engine, result) == 7
    events = load_events(engine)
    by_type = {event.event_type: event for event in events}

    assert set(by_type) == {event_type.value for event_type in ChangeEventType}
    assert {event.event_type: event.weight for event in events} == {
        "page_added": 2,
        "page_removed": 3,
        "title_changed": 3,
        "description_changed": 2,
        "h1_changed": 3,
        "text_changed": 3,
        "internal_links_changed": 3,
    }
    assert all(event.current_run_id == result.current_run_id for event in events)
    assert all(event.previous_run_id == result.previous_run_id for event in events)
    assert all(event.current_completed_at == UTC_COMPLETED_AT for event in events)
    assert by_type["page_added"].current_page_record_id is not None
    assert by_type["page_added"].previous_page_record_id is None
    assert by_type["page_removed"].current_page_record_id is None
    assert by_type["page_removed"].previous_page_record_id is not None
    for event_type in (
        "title_changed",
        "description_changed",
        "h1_changed",
        "text_changed",
        "internal_links_changed",
    ):
        assert by_type[event_type].current_page_record_id is not None
        assert by_type[event_type].previous_page_record_id is not None
    assert by_type["text_changed"].text_distance == 3
    assert by_type["text_changed"].change_ratio == Fraction(3, 10)
    assert by_type["internal_links_changed"].text_distance is None
    assert by_type["internal_links_changed"].change_ratio == Fraction(2, 3)
    assert by_type["title_changed"].change_ratio is None
    assert "previous_value" not in SnapshotChangeEvent.__table__.columns
    assert "current_value" not in SnapshotChangeEvent.__table__.columns
    assert "normalized_text" not in SnapshotChangeEvent.__table__.columns
    assert "internal_links" not in SnapshotChangeEvent.__table__.columns


def test_repeated_save_is_idempotent(engine) -> None:
    result = create_mixed_result(engine)

    assert save_snapshot_comparison_events(engine, result) == 7
    assert save_snapshot_comparison_events(engine, result) == 0

    assert len(load_events(engine)) == 7


def test_reopening_sqlite_preserves_events_and_exact_metrics(engine) -> None:
    result = create_mixed_result(engine)
    database_url = str(engine.url)
    save_snapshot_comparison_events(engine, result)
    engine.dispose()

    reopened = build_engine(database_url)
    events = load_events(reopened)
    by_type = {event.event_type: event for event in events}

    assert len(events) == 7
    assert by_type["text_changed"].text_distance == 3
    assert by_type["text_changed"].change_ratio == Fraction(3, 10)
    assert by_type["internal_links_changed"].change_ratio == Fraction(2, 3)
    assert by_type["page_added"].current_completed_at == UTC_COMPLETED_AT
    reopened.dispose()


def test_baseline_creates_no_events(engine) -> None:
    baseline = CompletedSnapshotComparisonResult(
        current_run_id=1,
        previous_run_id=None,
        current_completed_at=UTC_COMPLETED_AT,
        creates_baseline=True,
        new_pages=(),
        removed_pages=(),
        changed_pages=(),
    )

    assert save_snapshot_comparison_events(engine, baseline) == 0
    assert load_events(engine) == []


def test_database_error_rolls_back_the_whole_event_set(engine) -> None:
    with Session(engine) as session:
        site = Site(name="Ошибка", url="https://error.example")
        session.add(site)
        session.flush()
        assert site.id is not None
        previous_run_id = add_run(
            session,
            site.id,
            UTC_COMPLETED_AT - timedelta(hours=1),
        )
        current_run_id = add_run(session, site.id, UTC_COMPLETED_AT)
        valid_page_id = add_page(
            session,
            current_run_id,
            1,
            "https://error.example/valid",
        )
        invalid_page_id = add_page(
            session,
            current_run_id,
            2,
            "https://error.example/invalid",
        )
        session.commit()
    result = CompletedSnapshotComparisonResult(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        current_completed_at=UTC_COMPLETED_AT,
        creates_baseline=False,
        new_pages=(
            NewSnapshotPageComparison(
                current=SnapshotPageReference(
                    valid_page_id,
                    "https://error.example/valid",
                )
            ),
            NewSnapshotPageComparison(
                current=SnapshotPageReference(
                    invalid_page_id,
                    "https://error.example/invalid",
                ),
                importance=ChangeImportance.MEDIUM,
                weight=4,
            ),
        ),
        removed_pages=(),
        changed_pages=(),
    )

    with pytest.raises(IntegrityError):
        save_snapshot_comparison_events(engine, result)

    assert load_events(engine) == []


def add_single_event_site(engine, name: str) -> tuple[int, int]:
    with Session(engine) as session:
        site = Site(name=name, url=f"https://{name}.example")
        session.add(site)
        session.flush()
        assert site.id is not None
        site_id = site.id
        previous_run_id = add_run(
            session,
            site_id,
            UTC_COMPLETED_AT - timedelta(hours=1),
        )
        current_run_id = add_run(session, site_id, UTC_COMPLETED_AT)
        page_id = add_page(
            session,
            current_run_id,
            1,
            f"https://{name}.example/new",
        )
        session.commit()
    result = CompletedSnapshotComparisonResult(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        current_completed_at=UTC_COMPLETED_AT,
        creates_baseline=False,
        new_pages=(
            NewSnapshotPageComparison(
                current=SnapshotPageReference(
                    page_id,
                    f"https://{name}.example/new",
                )
            ),
        ),
        removed_pages=(),
        changed_pages=(),
    )
    save_snapshot_comparison_events(engine, result)
    return site_id, current_run_id


def test_site_deletion_removes_only_its_events_in_one_operation(engine) -> None:
    deleted_site_id, deleted_run_id = add_single_event_site(engine, "deleted")
    kept_site_id, kept_run_id = add_single_event_site(engine, "kept")

    assert delete_site(engine, deleted_site_id) is True

    with Session(engine) as session:
        assert session.get(Site, deleted_site_id) is None
        assert session.get(Site, kept_site_id) is not None
        assert session.get(CrawlRun, deleted_run_id) is None
        assert session.get(CrawlRun, kept_run_id) is not None
        remaining = session.exec(select(SnapshotChangeEvent)).one()
    assert remaining.current_run_id == kept_run_id


def test_model_has_portable_unique_pair_type_url_and_foreign_keys() -> None:
    constraints = {
        constraint.name for constraint in SnapshotChangeEvent.__table__.constraints
    }
    assert "uq_snapshot_change_event_pair_type_url" in constraints
    assert next(
        iter(SnapshotChangeEvent.__table__.c.current_run_id.foreign_keys)
    ).target_fullname == "crawlrun.id"
    assert next(
        iter(SnapshotChangeEvent.__table__.c.previous_run_id.foreign_keys)
    ).target_fullname == "crawlrun.id"
    assert next(
        iter(SnapshotChangeEvent.__table__.c.current_page_record_id.foreign_keys)
    ).target_fullname == "crawlpagerecord.id"
    assert next(
        iter(SnapshotChangeEvent.__table__.c.previous_page_record_id.foreign_keys)
    ).target_fullname == "crawlpagerecord.id"


def test_persistence_module_contains_no_sqlite_specific_sql() -> None:
    import marketing_intelligence.change_event_persistence as module

    module_path = Path(module.__file__)
    source = module_path.read_text(encoding="utf-8").lower()

    assert "sqlite" not in source
    assert "insert or" not in source
    assert "on conflict" not in source
