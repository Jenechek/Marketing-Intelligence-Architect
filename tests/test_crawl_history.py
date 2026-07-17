import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from marketing_intelligence.crawl_history import (
    recover_interrupted_runs,
    run_crawl,
    start_crawl_run,
)
from marketing_intelligence.completed_crawl_processing import (
    process_completed_crawl_run,
)
from marketing_intelligence.crawler import (
    CrawlCounters,
    CrawlPageResult,
    CrawlResult,
    CrawlSettings,
    CrawlStatus,
    PageOutcome,
)
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.models import (
    CrawlPagePriceRecord,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    Site,
    SnapshotChangeEvent,
)
from marketing_intelligence.page_content import PageData, PagePrice
from marketing_intelligence.price_persistence import (
    decode_decimal_text,
    encode_decimal_text,
)


def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'crawl.db').as_posix()}"


def initialized_engine(tmp_path: Path):
    engine = build_engine(database_url(tmp_path))
    initialize_database(engine)
    with Session(engine) as session:
        session.add(Site(name="Тест", url="https://example.com/"))
        session.commit()
    return engine


def saved_runs(engine) -> list[CrawlRun]:
    with Session(engine) as session:
        return list(session.exec(select(CrawlRun).order_by(CrawlRun.id)).all())


class ResultCrawler:
    def __init__(self, engine, result: CrawlResult) -> None:
        self.engine = engine
        self.result = result
        self.saw_running = False

    async def crawl(
        self, start_url: str, settings: CrawlSettings, *, progress=None
    ) -> CrawlResult:
        current = saved_runs(self.engine)
        self.saw_running = (
            len(current) == 1
            and current[0].status == "running"
            and current[0].completed_at is None
            and current[0].processed == 0
        )
        return self.result


def result_with(
    *,
    status: CrawlStatus = CrawlStatus.COMPLETED,
    pages: tuple[CrawlPageResult, ...] = (),
    counters: CrawlCounters = CrawlCounters(),
    robots_status: int | None = 200,
    limited: bool = False,
) -> CrawlResult:
    return CrawlResult(
        status=status,
        message="Результат обхода",
        robots_status=robots_status,
        pages=pages,
        counters=counters,
        limited=limited,
    )


def page_data(
    text: str = "текст",
    *,
    title: str | None = "Заголовок",
    description: str | None = "Описание",
    h1: str | None = "H1",
    links: tuple[str, ...] = (),
    checked_at: datetime = datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    prices: tuple[PagePrice, ...] = (),
) -> PageData:
    return PageData(
        checked_at=checked_at,
        title=title,
        description=description,
        h1=h1,
        normalized_text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        internal_links=links,
        prices=prices,
    )


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (result_with(), "completed"),
        (
            result_with(
                pages=(
                    CrawlPageResult(
                        "https://example.com/error",
                        0,
                        PageOutcome.HTTP_ERROR,
                        "Ошибка страницы",
                        500,
                    ),
                ),
                counters=CrawlCounters(processed=1, requested=1, errors=1),
            ),
            "partial",
        ),
        (
            result_with(
                status=CrawlStatus.ROBOTS_DEFERRED,
                robots_status=503,
            ),
            "deferred",
        ),
    ],
)
def test_running_is_created_before_crawler_and_expected_status_is_saved(
    tmp_path: Path,
    result: CrawlResult,
    expected_status: str,
) -> None:
    engine = initialized_engine(tmp_path)
    crawler = ResultCrawler(engine, result)

    saved = asyncio.run(
        run_crawl(engine, 1, "https://example.com/", crawler=crawler)
    )

    assert crawler.saw_running is True
    assert saved.status == expected_status
    assert saved.completed_at is not None


def test_unknown_site_does_not_create_records_or_call_crawler(tmp_path: Path) -> None:
    engine = initialized_engine(tmp_path)

    class TrackingCrawler:
        called = False

        async def crawl(
            self, start_url: str, settings: CrawlSettings, *, progress=None
        ) -> CrawlResult:
            self.called = True
            return result_with()

    crawler = TrackingCrawler()

    with pytest.raises(LookupError, match="Сайт для запуска обхода не найден"):
        asyncio.run(
            run_crawl(engine, 999, "https://unknown.example/", crawler=crawler)
        )

    with Session(engine) as session:
        runs = session.exec(select(CrawlRun)).all()
        pages = session.exec(select(CrawlPageRecord)).all()

    assert crawler.called is False
    assert runs == []
    assert pages == []


def test_settings_counters_and_ordered_page_metadata_survive_engine_restart(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    settings = CrawlSettings(
        max_pages=7,
        max_depth=2,
        delay=0.25,
        timeout=4.5,
        user_agent="PersistenceTest/1.0",
    )
    pages = (
        CrawlPageResult(
            "https://example.com/",
            0,
            PageOutcome.HTML,
            "HTML обработан",
            200,
            ("https://example.com/next",),
            page_data=page_data(links=("https://example.com/next",)),
        ),
        CrawlPageResult(
            "https://example.com/next",
            1,
            PageOutcome.FORBIDDEN,
            "Запрещено robots.txt",
        ),
    )
    crawler = ResultCrawler(
        engine,
        result_with(
            pages=pages,
            counters=CrawlCounters(
                processed=2,
                requested=1,
                successful=1,
                forbidden=1,
                errors=0,
            ),
            robots_status=200,
            limited=True,
        ),
    )

    asyncio.run(
        run_crawl(
            engine,
            1,
            "https://example.com/",
            crawler=crawler,
            settings=settings,
        )
    )
    engine.dispose()

    restarted = build_engine(database_url(tmp_path))
    initialize_database(restarted)
    with Session(restarted) as session:
        run = session.exec(select(CrawlRun)).one()
        records = list(
            session.exec(
                select(CrawlPageRecord).order_by(CrawlPageRecord.sequence_number)
            ).all()
        )

    assert (run.max_pages, run.max_depth, run.delay, run.timeout, run.user_agent) == (
        7,
        2,
        0.25,
        4.5,
        "PersistenceTest/1.0",
    )
    assert (
        run.robots_status,
        run.processed,
        run.requested,
        run.successful,
        run.forbidden,
        run.errors,
        run.limited,
    ) == (200, 2, 1, 1, 1, 0, True)
    assert [record.sequence_number for record in records] == [1, 2]
    assert [record.url for record in records] == [page.url for page in pages]
    assert [record.http_status for record in records] == [200, None]
    assert "discovered_links" not in CrawlPageRecord.__table__.columns
    assert "html" not in CrawlPageRecord.__table__.columns
    assert "text" not in CrawlPageRecord.__table__.columns
    restarted.dispose()


def test_completed_snapshot_preserves_missing_empty_utc_hash_and_ordered_unicode_links(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    links = (
        "https://example.com/ёж",
        "https://example.com/β",
        "https://example.com/ёж",
    )
    data = page_data(
        "ёжик и β",
        title=None,
        description="",
        h1="Заголовок",
        links=links,
    )
    page = CrawlPageResult(
        "https://example.com/",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
        links,
        page_data=data,
    )

    asyncio.run(
        run_crawl(
            engine,
            1,
            page.url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(page,),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )
    engine.dispose()

    restarted = build_engine(database_url(tmp_path))
    initialize_database(restarted)
    with Session(restarted) as session:
        record = session.exec(select(CrawlPageRecord)).one()
        snapshot = session.exec(select(CrawlPageSnapshot)).one()
        stored_prices = session.exec(select(CrawlPagePriceRecord)).all()

    assert snapshot.crawl_page_record_id == record.id
    assert (snapshot.title, snapshot.description, snapshot.h1) == (
        None,
        "",
        "Заголовок",
    )
    assert snapshot.checked_at == data.checked_at
    assert snapshot.checked_at.tzinfo is UTC
    assert snapshot.content_hash == hashlib.sha256("ёжик и β".encode("utf-8")).hexdigest()
    assert snapshot.internal_links_json == (
        '["https://example.com/ёж","https://example.com/β",'
        '"https://example.com/ёж"]'
    )
    assert tuple(json.loads(snapshot.internal_links_json)) == links
    assert "url" not in CrawlPageSnapshot.__table__.columns
    assert "http_status" not in CrawlPageSnapshot.__table__.columns
    assert stored_prices == []
    restarted.dispose()


def test_completed_saves_ordered_prices_with_exact_canonical_amounts(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    prices = (
        PagePrice(Decimal("10.00"), "RUB", "price", "json-ld"),
        PagePrice(Decimal("0.0100"), None, "low", "microdata"),
        PagePrice(Decimal("-0.000"), "USD", "high", "json-ld"),
    )
    page = CrawlPageResult(
        "https://example.com/",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
        page_data=page_data(prices=prices),
    )

    asyncio.run(
        run_crawl(
            engine,
            1,
            page.url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(page,),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )
    engine.dispose()

    restarted = build_engine(database_url(tmp_path))
    initialize_database(restarted)
    with Session(restarted) as session:
        stored = list(
            session.exec(
                select(CrawlPagePriceRecord).order_by(
                    CrawlPagePriceRecord.sequence_number
                )
            )
        )
        snapshot = session.exec(select(CrawlPageSnapshot)).one()

        assert [price.crawl_page_snapshot_id for price in stored] == [
            snapshot.crawl_page_record_id,
        ] * 3
        assert [price.sequence_number for price in stored] == [1, 2, 3]
        assert [price.amount_text for price in stored] == ["10", "0.01", "0"]
        assert [price.amount for price in stored] == [
            Decimal("10"),
            Decimal("0.01"),
            Decimal("0"),
        ]
        assert [price.currency for price in stored] == ["RUB", None, "USD"]
        assert [price.kind for price in stored] == ["price", "low", "high"]
        assert [price.source for price in stored] == [
            "json-ld",
            "microdata",
            "json-ld",
        ]
        foreign_key = next(
            iter(
                CrawlPagePriceRecord.__table__.c.crawl_page_snapshot_id.foreign_keys
            )
        )
        assert foreign_key.target_fullname == (
            "crawlpagesnapshot.crawl_page_record_id"
        )

        session.add(
            CrawlPagePriceRecord(
                crawl_page_snapshot_id=snapshot.crawl_page_record_id,
                sequence_number=1,
                amount_text="1",
                currency=None,
                kind="price",
                source="test",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    restarted.dispose()


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("10.00"), "10"),
        (Decimal("0.0100"), "0.01"),
        (Decimal("0"), "0"),
        (Decimal("-0.000"), "0"),
        (Decimal("1E+3"), "1000"),
    ],
)
def test_decimal_text_round_trip_is_exact_and_canonical(
    amount: Decimal,
    expected: str,
) -> None:
    encoded = encode_decimal_text(amount)

    assert encoded == expected
    assert decode_decimal_text(encoded) == amount


@pytest.mark.parametrize(
    "value",
    ["+1", "1.0", "1e2", "01", "-1", "NaN", "١"],
)
def test_decimal_restore_rejects_noncanonical_text(value: str) -> None:
    with pytest.raises(ValueError, match="каноническим"):
        decode_decimal_text(value)


def test_partial_saves_success_snapshot_and_error_without_replacing_previous_run(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    first_page = CrawlPageResult(
        "https://example.com/old",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
        page_data=page_data(
            "старый текст",
            prices=(PagePrice(Decimal("100"), "RUB", "price", "json-ld"),),
        ),
    )
    asyncio.run(
        run_crawl(
            engine,
            1,
            first_page.url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(first_page,),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )

    new_page = CrawlPageResult(
        "https://example.com/new",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
        page_data=page_data(
            "новый текст",
            prices=(PagePrice(Decimal("200"), None, "price", "microdata"),),
        ),
    )
    error_page = CrawlPageResult(
        "https://example.com/error",
        1,
        PageOutcome.HTTP_ERROR,
        "Ошибка страницы",
        503,
    )
    second = asyncio.run(
        run_crawl(
            engine,
            1,
            new_page.url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(new_page, error_page),
                    counters=CrawlCounters(
                        processed=2,
                        requested=2,
                        successful=1,
                        errors=1,
                    ),
                ),
            ),
        )
    )

    with Session(engine) as session:
        runs = list(session.exec(select(CrawlRun).order_by(CrawlRun.id)))
        records = list(session.exec(select(CrawlPageRecord).order_by(CrawlPageRecord.id)))
        snapshots = list(
            session.exec(select(CrawlPageSnapshot).order_by(CrawlPageSnapshot.crawl_page_record_id))
        )
        stored_prices = list(
            session.exec(
                select(CrawlPagePriceRecord).order_by(CrawlPagePriceRecord.id)
            )
        )

    assert second.status == "partial"
    assert [run.status for run in runs] == ["completed", "partial"]
    assert [record.url for record in records] == [
        first_page.url,
        new_page.url,
        error_page.url,
    ]
    assert [snapshot.normalized_text for snapshot in snapshots] == [
        "старый текст",
        "новый текст",
    ]
    assert records[-1].outcome == "http_error"
    assert [price.amount_text for price in stored_prices] == ["100", "200"]


def test_completed_runs_automatically_create_exact_events_and_are_idempotent(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    url = "https://example.com/"
    first = asyncio.run(
        run_crawl(
            engine,
            1,
            url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(
                        CrawlPageResult(
                            url,
                            0,
                            PageOutcome.HTML,
                            "HTML обработан",
                            200,
                            page_data=page_data(
                                "старый текст",
                                title="Старый Title",
                                description="Старое описание",
                                h1="Старый H1",
                                links=("https://example.com/old",),
                            ),
                        ),
                    ),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )

    with Session(engine) as session:
        assert session.exec(select(SnapshotChangeEvent)).all() == []
    assert process_completed_crawl_run(engine, first.id) == 0

    second = asyncio.run(
        run_crawl(
            engine,
            1,
            url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(
                        CrawlPageResult(
                            url,
                            0,
                            PageOutcome.HTML,
                            "HTML обработан",
                            200,
                            page_data=page_data(
                                "новый текст",
                                title="Новый Title",
                                description="Новое описание",
                                h1="Новый H1",
                                links=("https://example.com/new",),
                            ),
                        ),
                    ),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )

    with Session(engine) as session:
        events = list(
            session.exec(
                select(SnapshotChangeEvent).order_by(SnapshotChangeEvent.event_type)
            )
        )
    assert {event.event_type for event in events} == {
        "title_changed",
        "description_changed",
        "h1_changed",
        "text_changed",
        "internal_links_changed",
    }
    assert all(event.current_run_id == second.id for event in events)
    assert all(event.previous_run_id == first.id for event in events)
    assert process_completed_crawl_run(engine, second.id) == 0
    with Session(engine) as session:
        assert len(session.exec(select(SnapshotChangeEvent)).all()) == 5


def test_partial_between_completed_runs_is_not_processed_or_used_as_baseline(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    url = "https://example.com/"

    def completed_page(text: str) -> CrawlPageResult:
        return CrawlPageResult(
            url,
            0,
            PageOutcome.HTML,
            "HTML обработан",
            200,
            page_data=page_data(text, title=text),
        )

    first = asyncio.run(
        run_crawl(
            engine,
            1,
            url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(completed_page("первая версия"),),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )
    partial = asyncio.run(
        run_crawl(
            engine,
            1,
            url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(
                        completed_page("частичная версия"),
                        CrawlPageResult(
                            "https://example.com/error",
                            1,
                            PageOutcome.HTTP_ERROR,
                            "Ошибка страницы",
                            503,
                        ),
                    ),
                    counters=CrawlCounters(
                        processed=2,
                        requested=2,
                        successful=1,
                        errors=1,
                    ),
                ),
            ),
        )
    )
    second = asyncio.run(
        run_crawl(
            engine,
            1,
            url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    pages=(completed_page("вторая версия"),),
                    counters=CrawlCounters(processed=1, requested=1, successful=1),
                ),
            ),
        )
    )

    with Session(engine) as session:
        events = list(session.exec(select(SnapshotChangeEvent)))
    assert partial.status == "partial"
    assert events
    assert all(event.current_run_id == second.id for event in events)
    assert all(event.previous_run_id == first.id for event in events)
    assert all(event.current_run_id != partial.id for event in events)


def test_post_processing_error_preserves_completed_run_pages_and_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = initialized_engine(tmp_path)
    page = CrawlPageResult(
        "https://example.com/",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
        page_data=page_data("сохранённый текст"),
    )

    def fail_comparison(_comparison_input):
        raise RuntimeError("сбой постобработки")

    monkeypatch.setattr(
        "marketing_intelligence.completed_crawl_processing."
        "build_completed_snapshot_comparison",
        fail_comparison,
    )

    with pytest.raises(RuntimeError, match="сбой постобработки"):
        asyncio.run(
            run_crawl(
                engine,
                1,
                page.url,
                crawler=ResultCrawler(
                    engine,
                    result_with(
                        pages=(page,),
                        counters=CrawlCounters(
                            processed=1,
                            requested=1,
                            successful=1,
                        ),
                    ),
                ),
            )
        )

    with Session(engine) as session:
        run = session.exec(select(CrawlRun)).one()
        record = session.exec(select(CrawlPageRecord)).one()
        snapshot = session.exec(select(CrawlPageSnapshot)).one()
        events = session.exec(select(SnapshotChangeEvent)).all()
    assert run.status == "completed"
    assert record.url == page.url
    assert snapshot.normalized_text == "сохранённый текст"
    assert events == []


def test_deferred_failed_and_interrupted_runs_publish_no_snapshots(tmp_path: Path) -> None:
    engine = initialized_engine(tmp_path)
    deferred_page = CrawlPageResult(
        "https://example.com/deferred",
        0,
        PageOutcome.HTML,
        "Не должен публиковаться",
        200,
        page_data=page_data(
            prices=(PagePrice(Decimal("1"), "RUB", "price", "json-ld"),)
        ),
    )
    deferred = asyncio.run(
        run_crawl(
            engine,
            1,
            deferred_page.url,
            crawler=ResultCrawler(
                engine,
                result_with(
                    status=CrawlStatus.ROBOTS_DEFERRED,
                    pages=(deferred_page,),
                ),
            ),
        )
    )

    class FailingCrawler:
        async def crawl(self, start_url, settings, *, progress=None):
            raise RuntimeError("сбой")

    with pytest.raises(RuntimeError, match="сбой"):
        asyncio.run(
            run_crawl(
                engine,
                1,
                "https://example.com/failed",
                crawler=FailingCrawler(),
            )
        )
    interrupted = start_crawl_run(engine, 1, CrawlSettings(delay=0))
    assert recover_interrupted_runs(engine) == 1

    with Session(engine) as session:
        runs = list(session.exec(select(CrawlRun).order_by(CrawlRun.id)))
        records = list(session.exec(select(CrawlPageRecord)))
        snapshots = list(session.exec(select(CrawlPageSnapshot)))
        prices = list(session.exec(select(CrawlPagePriceRecord)))
        events = list(session.exec(select(SnapshotChangeEvent)))

    assert deferred.status == "deferred"
    assert [run.status for run in runs] == ["deferred", "failed", "interrupted"]
    assert interrupted.id == runs[-1].id
    assert records == []
    assert snapshots == []
    assert prices == []
    assert events == []


def test_atomic_finalization_rolls_back_page_when_html_data_is_missing(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    invalid_page = CrawlPageResult(
        "https://example.com/",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
    )

    with pytest.raises(ValueError, match="не содержит извлечённых данных"):
        asyncio.run(
            run_crawl(
                engine,
                1,
                invalid_page.url,
                crawler=ResultCrawler(
                    engine,
                    result_with(
                        pages=(invalid_page,),
                        counters=CrawlCounters(
                            processed=1,
                            requested=1,
                            successful=1,
                        ),
                    ),
                ),
            )
        )

    with Session(engine) as session:
        run = session.exec(select(CrawlRun)).one()
        assert session.exec(select(CrawlPageRecord)).all() == []
        assert session.exec(select(CrawlPageSnapshot)).all() == []
    assert run.status == "failed"


def test_atomic_finalization_rolls_back_pages_snapshots_and_prices(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    invalid_page = CrawlPageResult(
        "https://example.com/",
        0,
        PageOutcome.HTML,
        "HTML обработан",
        200,
        page_data=page_data(
            prices=(
                PagePrice(Decimal("10"), "RUB", "price", "json-ld"),
                PagePrice(Decimal("NaN"), "RUB", "price", "json-ld"),
            )
        ),
    )

    with pytest.raises(ValueError, match="конечным неотрицательным"):
        asyncio.run(
            run_crawl(
                engine,
                1,
                invalid_page.url,
                crawler=ResultCrawler(
                    engine,
                    result_with(
                        pages=(invalid_page,),
                        counters=CrawlCounters(
                            processed=1,
                            requested=1,
                            successful=1,
                        ),
                    ),
                ),
            )
        )

    with Session(engine) as session:
        run = session.exec(select(CrawlRun)).one()
        assert session.exec(select(CrawlPageRecord)).all() == []
        assert session.exec(select(CrawlPageSnapshot)).all() == []
        assert session.exec(select(CrawlPagePriceRecord)).all() == []
    assert run.status == "failed"


def test_existing_database_gets_price_table_without_losing_data(
    tmp_path: Path,
) -> None:
    engine = build_engine(database_url(tmp_path))
    Site.__table__.create(engine)
    CrawlRun.__table__.create(engine)
    CrawlPageRecord.__table__.create(engine)
    CrawlPageSnapshot.__table__.create(engine)
    with Session(engine) as session:
        site = Site(name="Старый сайт", url="https://old.example/")
        session.add(site)
        session.flush()
        run = CrawlRun(
            site_id=site.id,
            status="completed",
            message="Старый запуск",
            max_pages=1,
            max_depth=0,
            delay=0,
            timeout=5,
            user_agent="Legacy/1.0",
        )
        session.add(run)
        session.flush()
        page = CrawlPageRecord(
                crawl_run_id=run.id,
                sequence_number=1,
                url="https://old.example/",
                depth=0,
                outcome="html",
                message="Старая запись",
                http_status=200,
            )
        session.add(page)
        session.flush()
        session.add(
            CrawlPageSnapshot(
                crawl_page_record_id=page.id,
                checked_at=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
                title="Старый заголовок",
                description=None,
                h1=None,
                normalized_text="Старый текст",
                content_hash=hashlib.sha256("Старый текст".encode("utf-8")).hexdigest(),
                internal_links_json="[]",
            )
        )
        session.commit()

    assert "crawlpagepricerecord" not in inspect(engine).get_table_names()
    initialize_database(engine)
    assert "crawlpagepricerecord" in inspect(engine).get_table_names()
    with Session(engine) as session:
        assert session.exec(select(Site)).one().name == "Старый сайт"
        assert session.exec(select(CrawlRun)).one().message == "Старый запуск"
        assert session.exec(select(CrawlPageRecord)).one().message == "Старая запись"
        assert session.exec(select(CrawlPageSnapshot)).one().title == "Старый заголовок"
        assert session.exec(select(CrawlPagePriceRecord)).all() == []


def test_unexpected_exception_is_saved_as_failed_and_reraised(tmp_path: Path) -> None:
    engine = initialized_engine(tmp_path)

    class FailingCrawler:
        async def crawl(
            self, start_url: str, settings: CrawlSettings, *, progress=None
        ) -> CrawlResult:
            raise RuntimeError("сбой транспорта")

    with pytest.raises(RuntimeError, match="сбой транспорта"):
        asyncio.run(
            run_crawl(engine, 1, "https://example.com/", crawler=FailingCrawler())
        )

    run = saved_runs(engine)[0]
    assert run.status == "failed"
    assert run.completed_at is not None
    assert "сбой транспорта" in run.message
    assert run.processed == 0


def test_restart_recovers_only_running_as_interrupted_and_preserves_missing_values(
    tmp_path: Path,
) -> None:
    engine = initialized_engine(tmp_path)
    running = start_crawl_run(engine, 1, CrawlSettings(delay=0))
    engine.dispose()

    restarted = build_engine(database_url(tmp_path))
    initialize_database(restarted)
    assert recover_interrupted_runs(restarted) == 1
    assert recover_interrupted_runs(restarted) == 0

    with Session(restarted) as session:
        recovered = session.get(CrawlRun, running.id)

    assert recovered is not None
    assert recovered.status == "interrupted"
    assert recovered.completed_at is not None
    assert recovered.processed == 0
    assert recovered.limited is None
    restarted.dispose()
