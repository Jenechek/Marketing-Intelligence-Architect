from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session

from marketing_intelligence.change_event import ChangeEventType
from marketing_intelligence.change_event_persistence import (
    ChangeEventType as PersistenceChangeEventType,
)
from marketing_intelligence.change_event_query import load_change_events
from marketing_intelligence.change_importance import ChangeImportance
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.models import CrawlRun, Site, SnapshotChangeEvent


BASE_TIME = datetime(2026, 7, 17, 10, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path):
    result = build_engine(f"sqlite:///{(tmp_path / 'query.db').as_posix()}")
    initialize_database(result)
    yield result
    result.dispose()


def _add_site(session: Session, name: str) -> int:
    site = Site(name=name, url=f"https://{name}.example")
    session.add(site)
    session.flush()
    assert site.id is not None
    return site.id


def _add_run(session: Session, site_id: int, completed_at: datetime) -> int:
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


def _add_event(
    session: Session,
    *,
    current_run_id: int,
    previous_run_id: int,
    completed_at: datetime,
    event_type: ChangeEventType,
    importance: ChangeImportance,
    url: str,
    weight: int,
    current_page_id: int | None = None,
    previous_page_id: int | None = None,
    distance: int | None = None,
    ratio: Fraction | None = None,
) -> int:
    stored = SnapshotChangeEvent(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        current_page_record_id=current_page_id,
        previous_page_record_id=previous_page_id,
        event_type=event_type.value,
        url=url,
        current_completed_at=completed_at,
        importance=importance.value,
        weight=weight,
        text_distance=distance,
        change_ratio_numerator=ratio.numerator if ratio is not None else None,
        change_ratio_denominator=ratio.denominator if ratio is not None else None,
    )
    session.add(stored)
    session.flush()
    assert stored.id is not None
    return stored.id


def _seed(engine):
    with Session(engine) as session:
        first_site = _add_site(session, "first")
        second_site = _add_site(session, "second")
        first_previous = _add_run(session, first_site, BASE_TIME - timedelta(days=1))
        second_previous = _add_run(session, second_site, BASE_TIME - timedelta(days=1))
        first_current = _add_run(session, first_site, BASE_TIME)
        second_current = _add_run(session, second_site, BASE_TIME)
        ids = [
            _add_event(
                session,
                current_run_id=first_current,
                previous_run_id=first_previous,
                completed_at=BASE_TIME,
                event_type=ChangeEventType.TEXT_CHANGED,
                importance=ChangeImportance.HIGH,
                url="https://first.example/text",
                weight=3,
                current_page_id=101,
                previous_page_id=91,
                distance=7,
                ratio=Fraction(2, 3),
            ),
            _add_event(
                session,
                current_run_id=first_current,
                previous_run_id=first_previous,
                completed_at=BASE_TIME,
                event_type=ChangeEventType.DESCRIPTION_CHANGED,
                importance=ChangeImportance.MEDIUM,
                url="https://first.example/description",
                weight=2,
            ),
            _add_event(
                session,
                current_run_id=second_current,
                previous_run_id=second_previous,
                completed_at=BASE_TIME,
                event_type=ChangeEventType.TEXT_CHANGED,
                importance=ChangeImportance.HIGH,
                url="https://second.example/text",
                weight=3,
            ),
        ]
        session.commit()
    return first_site, second_site, ids


def test_query_isolates_sites_and_returns_exact_immutable_dtos(engine) -> None:
    first_site, _, ids = _seed(engine)

    page = load_change_events(engine, site_id=first_site)

    assert page.total_count == 2
    assert [item.event_id for item in page.items] == [ids[1], ids[0]]
    assert {item.url for item in page.items} == {
        "https://first.example/text",
        "https://first.example/description",
    }
    text_item = page.items[1]
    assert text_item.event_type is ChangeEventType.TEXT_CHANGED
    assert text_item.importance is ChangeImportance.HIGH
    assert text_item.text_distance == 7
    assert text_item.change_ratio == Fraction(2, 3)
    assert text_item.current_page_record_id == 101
    assert text_item.previous_page_record_id == 91
    assert page.items[0].current_page_record_id is None
    assert page.items[0].previous_page_record_id is None
    with pytest.raises(FrozenInstanceError):
        page.total_count = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        text_item.weight = 1  # type: ignore[misc]
    assert PersistenceChangeEventType is ChangeEventType


def test_type_and_importance_filters_work_separately_and_together(engine) -> None:
    first_site, _, _ = _seed(engine)

    by_type = load_change_events(
        engine,
        site_id=first_site,
        event_types={ChangeEventType.TEXT_CHANGED},
    )
    by_importance = load_change_events(
        engine,
        site_id=first_site,
        importance_levels={ChangeImportance.MEDIUM},
    )
    combined = load_change_events(
        engine,
        site_id=first_site,
        event_types={ChangeEventType.TEXT_CHANGED},
        importance_levels={ChangeImportance.MEDIUM},
    )

    assert [item.event_type for item in by_type.items] == [ChangeEventType.TEXT_CHANGED]
    assert [item.importance for item in by_importance.items] == [ChangeImportance.MEDIUM]
    assert combined.items == ()
    assert combined.total_count == 0


def test_time_range_is_half_open_and_normalizes_timezone(engine) -> None:
    first_site, _, _ = _seed(engine)
    plus_three = timezone(timedelta(hours=3))

    included_at_from = load_change_events(
        engine,
        site_id=first_site,
        from_time=datetime(2026, 7, 17, 13, tzinfo=plus_three),
        before_time=datetime(2026, 7, 17, 14, tzinfo=plus_three),
    )
    excluded_at_before = load_change_events(
        engine,
        site_id=first_site,
        from_time=BASE_TIME - timedelta(hours=1),
        before_time=BASE_TIME,
    )

    assert included_at_from.total_count == 2
    assert all(item.current_completed_at == BASE_TIME for item in included_at_from.items)
    assert excluded_at_before.total_count == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"from_time": datetime(2026, 7, 17, 10)}, "часовой пояс"),
        ({"before_time": datetime(2026, 7, 17, 10)}, "часовой пояс"),
        ({"from_time": BASE_TIME, "before_time": BASE_TIME}, "раньше"),
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"offset": -1}, "offset"),
    ],
)
def test_invalid_inputs_are_rejected_before_query(engine, kwargs, message) -> None:
    statements = 0

    def count_statement(*_args) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        with pytest.raises(ValueError, match=message):
            load_change_events(engine, site_id=1, **kwargs)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert statements == 0


def test_stable_order_pagination_total_and_two_queries(engine) -> None:
    first_site, _, ids = _seed(engine)
    statements = 0

    def count_statement(*_args) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        page = load_change_events(engine, site_id=first_site, limit=1, offset=1)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert page.total_count == 2
    assert page.limit == 1
    assert page.offset == 1
    assert [item.event_id for item in page.items] == [ids[0]]
    assert statements == 2


def test_empty_and_unknown_sites_return_empty_page(engine) -> None:
    with Session(engine) as session:
        empty_site = _add_site(session, "empty")
        session.commit()

    assert load_change_events(engine, site_id=empty_site).items == ()
    unknown = load_change_events(engine, site_id=999_999)
    assert unknown.items == ()
    assert unknown.total_count == 0


def test_reading_does_not_change_sqlite_file(tmp_path: Path) -> None:
    database_path = tmp_path / "read-only.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = build_engine(database_url)
    initialize_database(engine)
    site_id, _, _ = _seed(engine)
    engine.dispose()
    before = sha256(database_path.read_bytes()).hexdigest()

    reopened = build_engine(database_url)
    assert load_change_events(reopened, site_id=site_id).total_count == 2
    reopened.dispose()

    assert sha256(database_path.read_bytes()).hexdigest() == before
