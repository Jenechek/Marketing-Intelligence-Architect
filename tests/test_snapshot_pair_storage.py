from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func
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
    InternalLinkNotStringError,
    InternalLinksNotArrayError,
    InvalidInternalLinksJsonError,
    load_completed_snapshot_comparison_input,
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
    checked_at: datetime = BASE_TIME,
    title: str | None = None,
    description: str | None = None,
    h1: str | None = None,
    normalized_text: str = "text",
    content_hash: str = "hash",
    internal_links_json: str = "[]",
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
                checked_at=checked_at,
                title=title,
                description=description,
                h1=h1,
                normalized_text=normalized_text,
                content_hash=content_hash,
                internal_links_json=internal_links_json,
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


def test_full_first_completed_run_is_baseline_without_new_or_matched_pages(
    engine,
) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        run_id = add_run(session, site_id)
        add_page(session, run_id, "https://example.com/baseline")
        session.commit()

    result = load_completed_snapshot_comparison_input(engine, run_id)

    assert result.current_run_id == run_id
    assert result.previous_run_id is None
    assert result.current_completed_at == BASE_TIME
    assert result.creates_baseline is True
    assert result.new_pages == ()
    assert result.removed_pages == ()
    assert result.matched_pages == ()


def test_full_pair_loads_new_removed_and_every_matched_page_field(engine) -> None:
    previous_checked_at = BASE_TIME - timedelta(minutes=2)
    current_checked_at = BASE_TIME + timedelta(hours=1, minutes=2)
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        removed_page_id = add_page(
            session, previous_id, "https://example.com/removed"
        )
        previous_page_id = add_page(
            session,
            previous_id,
            "https://example.com/same",
            checked_at=previous_checked_at,
            title=None,
            description="",
            h1="Старый H1",
            normalized_text="Старый текст ё",
            content_hash="old-hash",
            internal_links_json='["/юникод","/a","/юникод"]',
        )
        current_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=1)
        )
        new_page_id = add_page(session, current_id, "https://example.com/new")
        current_page_id = add_page(
            session,
            current_id,
            "https://example.com/same",
            checked_at=current_checked_at,
            title="",
            description=None,
            h1="Новый H1",
            normalized_text="Новый текст ё",
            content_hash="new-hash",
            internal_links_json='["/z","/a","/z"]',
        )
        session.commit()

    result = load_completed_snapshot_comparison_input(engine, current_id)

    assert result.current_run_id == current_id
    assert result.previous_run_id == previous_id
    assert result.current_completed_at == BASE_TIME + timedelta(hours=1)
    assert result.creates_baseline is False
    assert tuple((page.identifier, page.url) for page in result.new_pages) == (
        (new_page_id, "https://example.com/new"),
    )
    assert tuple((page.identifier, page.url) for page in result.removed_pages) == (
        (removed_page_id, "https://example.com/removed"),
    )
    assert len(result.matched_pages) == 1
    matched = result.matched_pages[0]
    assert matched.url == "https://example.com/same"
    assert (
        matched.previous.identifier,
        matched.previous.url,
        matched.previous.checked_at,
        matched.previous.title,
        matched.previous.description,
        matched.previous.h1,
        matched.previous.normalized_text,
        matched.previous.content_hash,
        matched.previous.internal_links,
    ) == (
        previous_page_id,
        "https://example.com/same",
        previous_checked_at,
        None,
        "",
        "Старый H1",
        "Старый текст ё",
        "old-hash",
        ("/юникод", "/a", "/юникод"),
    )
    assert (
        matched.current.identifier,
        matched.current.checked_at,
        matched.current.title,
        matched.current.description,
        matched.current.h1,
        matched.current.normalized_text,
        matched.current.content_hash,
        matched.current.internal_links,
    ) == (
        current_page_id,
        current_checked_at,
        "",
        None,
        "Новый H1",
        "Новый текст ё",
        "new-hash",
        ("/z", "/a", "/z"),
    )
    with pytest.raises(FrozenInstanceError):
        matched.current.title = "изменено"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("stored_value", "error_type", "message"),
    [
        ("{", InvalidInternalLinksJsonError, "повреждённый JSON"),
        ('{"url":"/a"}', InternalLinksNotArrayError, "JSON-массивом"),
        ('["/a",1]', InternalLinkNotStringError, "только строки"),
    ],
)
def test_full_pair_rejects_each_invalid_internal_links_shape(
    engine, stored_value, error_type, message
) -> None:
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        page_id = add_page(
            session,
            previous_id,
            "https://example.com/same",
            internal_links_json=stored_value,
        )
        current_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=1)
        )
        add_page(session, current_id, "https://example.com/same")
        session.commit()

    with pytest.raises(error_type, match=message) as error:
        load_completed_snapshot_comparison_input(engine, current_id)

    assert error.value.page_identifier == page_id
    assert str(page_id) in str(error.value)


def test_full_pair_orders_multiple_matches_by_url_and_loads_in_one_batch(engine) -> None:
    urls = (
        "https://example.com/юникод",
        "https://example.com/a",
        "https://example.com/z",
    )
    with Session(engine) as session:
        site_id = add_site(session)
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        for url in urls:
            add_page(session, previous_id, url)
        current_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=1)
        )
        for url in reversed(urls):
            add_page(session, current_id, url)
        session.commit()

    selects: list[str] = []

    def count_selects(_connection, _cursor, statement, *_args) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        result = load_completed_snapshot_comparison_input(engine, current_id)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert tuple(page.url for page in result.matched_pages) == tuple(sorted(urls))
    assert len(selects) == 3


def test_full_pair_preserves_selection_rules_and_sqlite_after_reopening(engine) -> None:
    database_url = str(engine.url)
    with Session(engine) as session:
        site_id = add_site(session, "main")
        other_site_id = add_site(session, "other")
        previous_id = add_run(session, site_id, completed_at=BASE_TIME)
        add_page(session, previous_id, "https://example.com/old")
        add_run(
            session,
            site_id,
            status="failed",
            completed_at=BASE_TIME + timedelta(hours=1),
        )
        add_run(
            session,
            other_site_id,
            completed_at=BASE_TIME + timedelta(hours=2),
        )
        current_id = add_run(
            session, site_id, completed_at=BASE_TIME + timedelta(hours=3)
        )
        add_page(session, current_id, "https://example.com/new")
        session.commit()
    before = _database_counts(engine)
    engine.dispose()

    reopened = build_engine(database_url)
    result = load_completed_snapshot_comparison_input(reopened, current_id)
    reopened.dispose()
    reopened_again = build_engine(database_url)

    assert result.previous_run_id == previous_id
    assert _database_counts(reopened_again) == before
    reopened_again.dispose()


def _database_counts(engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        return (
            session.exec(select(func.count()).select_from(Site)).one(),
            session.exec(select(func.count()).select_from(CrawlRun)).one(),
            session.exec(select(func.count()).select_from(CrawlPageRecord)).one(),
            session.exec(select(func.count()).select_from(CrawlPageSnapshot)).one(),
        )
