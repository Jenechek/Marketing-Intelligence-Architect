from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func
from sqlmodel import Session, select

from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.models import (
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    Site,
)
from marketing_intelligence.snapshot_pair_storage import (
    CrawlRunNotCompletedError,
    CrawlRunNotFoundError,
    load_completed_snapshot_pair,
)


BASE_TIME = datetime(2026, 7, 16, 10, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    database = tmp_path / "snapshot-pairs.db"
    result = build_engine(f"sqlite:///{database.as_posix()}")
    initialize_database(result)
    return result


def add_site(session: Session, name: str = "Сайт") -> int:
    site = Site(name=name, url=f"https://{name}.example")
    session.add(site)
    session.flush()
    assert site.id is not None
    return site.id


def add_run(
    session: Session,
    site_id: int,
    *,
    status: str = "completed",
    completed_at: datetime | None = BASE_TIME,
) -> int:
    run = CrawlRun(
        site_id=site_id,
        status=status,
        message=status,
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
    url: str,
    *,
    snapshot: bool = True,
) -> int:
    record = CrawlPageRecord(
        crawl_run_id=run_id,
        sequence_number=1,
        url=url,
        depth=0,
        outcome="html" if snapshot else "http_error",
        message="ok" if snapshot else "error",
    )
    session.add(record)
    session.flush()
    assert record.id is not None
    if snapshot:
        session.add(
            CrawlPageSnapshot(
                crawl_page_record_id=record.id,
                checked_at=BASE_TIME,
                normalized_text="text",
                content_hash="hash",
                internal_links_json="[]",
            )
        )
    return record.id


def result_urls(values) -> tuple[str, ...]:
    return tuple(value.url for value in values)


def test_first_completed_run_creates_baseline_without_false_new_pages(engine) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        run_id = add_run(session, site_id)
        page_id = add_page(session, run_id, "https://example.com/StoredPath")
        session.commit()

    pair = load_completed_snapshot_pair(engine, run_id)

    assert pair.current_run_id == run_id
    assert pair.previous_run_id is None
    assert pair.match_result.creates_baseline is True
    assert pair.match_result.current_only == ()
    assert result_urls(pair.match_result.baseline_pages) == (
        "https://example.com/StoredPath",
    )
    assert pair.match_result.baseline_pages[0].identifier == page_id
    with pytest.raises(FrozenInstanceError):
        pair.previous_run_id = run_id  # type: ignore[misc]


@pytest.mark.parametrize(
    "status",
    ["running", "partial", "failed", "deferred", "interrupted"],
)
def test_previous_selection_skips_every_unsuitable_status(engine, status) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        add_run(
            session,
            site_id,
            status=status,
            completed_at=BASE_TIME + timedelta(hours=1),
        )
        current_id = add_run(
            session,
            site_id,
            completed_at=BASE_TIME + timedelta(hours=2),
        )
        session.commit()

    assert load_completed_snapshot_pair(engine, current_id).previous_run_id == previous_id


def test_previous_selection_ignores_other_site(engine) -> None:
    with Session(engine) as session:
        first_site = add_site(session, "first")
        second_site = add_site(session, "second")
        previous_id = add_run(session, first_site, completed_at=BASE_TIME)
        add_run(
            session,
            second_site,
            completed_at=BASE_TIME + timedelta(hours=1),
        )
        current_id = add_run(
            session,
            first_site,
            completed_at=BASE_TIME + timedelta(hours=2),
        )
        session.commit()

    assert load_completed_snapshot_pair(engine, current_id).previous_run_id == previous_id


def test_previous_selection_orders_by_completed_at_then_id(engine) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        add_run(session, site_id, completed_at=BASE_TIME - timedelta(hours=1))
        first_same_time_id = add_run(session, site_id, completed_at=BASE_TIME)
        second_same_time_id = add_run(session, site_id, completed_at=BASE_TIME)
        current_id = add_run(session, site_id, completed_at=BASE_TIME)
        session.commit()

    pair = load_completed_snapshot_pair(engine, current_id)

    assert second_same_time_id > first_same_time_id
    assert pair.previous_run_id == second_same_time_id


def test_selection_is_relative_to_non_latest_current_run(engine) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        selected_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=1)
        )
        add_run(session, site_id, completed_at=BASE_TIME + timedelta(hours=2))
        session.commit()

    pair = load_completed_snapshot_pair(engine, selected_id)

    assert pair.current_run_id == selected_id
    assert pair.previous_run_id == previous_id


def test_loads_only_records_with_snapshots_and_matches_stored_urls(engine) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        removed_id = add_page(session, previous_id, "https://example.com/removed")
        matched_previous_id = add_page(session, previous_id, "https://example.com/same")
        add_page(session, previous_id, "https://example.com/no-old-snapshot", snapshot=False)
        current_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=1)
        )
        new_id = add_page(session, current_id, "https://EXAMPLE.com/New")
        matched_current_id = add_page(session, current_id, "https://example.com/same")
        add_page(session, current_id, "https://example.com/no-new-snapshot", snapshot=False)
        session.commit()

    result = load_completed_snapshot_pair(engine, current_id).match_result

    assert result.creates_baseline is False
    assert result_urls(result.current_only) == ("https://EXAMPLE.com/New",)
    assert result.current_only[0].identifier == new_id
    assert result_urls(result.previous_only) == ("https://example.com/removed",)
    assert result.previous_only[0].identifier == removed_id
    assert tuple(match.url for match in result.matched) == ("https://example.com/same",)
    assert result.matched[0].current.identifier == matched_current_id
    assert result.matched[0].previous.identifier == matched_previous_id


def test_missing_current_run_has_clear_error(engine) -> None:
    with pytest.raises(CrawlRunNotFoundError, match="999.*не найден") as error:
        load_completed_snapshot_pair(engine, 999)

    assert error.value.run_id == 999


@pytest.mark.parametrize(
    ("status", "completed_at"),
    [("running", None), ("partial", BASE_TIME), ("completed", None)],
)
def test_unsuitable_current_run_has_clear_error(engine, status, completed_at) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        run_id = add_run(
            session,
            site_id,
            status=status,
            completed_at=completed_at,
        )
        session.commit()

    with pytest.raises(CrawlRunNotCompletedError, match=str(run_id)) as error:
        load_completed_snapshot_pair(engine, run_id)

    assert error.value.run_id == run_id


def test_loading_pair_does_not_change_persisted_data(engine) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        add_page(session, previous_id, "https://example.com/old")
        current_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=1)
        )
        add_page(session, current_id, "https://example.com/new")
        session.commit()

    before = _database_counts(engine)
    load_completed_snapshot_pair(engine, current_id)
    after = _database_counts(engine)

    assert after == before


def _database_counts(engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        return (
            session.exec(select(func.count()).select_from(Site)).one(),
            session.exec(select(func.count()).select_from(CrawlRun)).one(),
            session.exec(select(func.count()).select_from(CrawlPageRecord)).one(),
            session.exec(select(func.count()).select_from(CrawlPageSnapshot)).one(),
        )
