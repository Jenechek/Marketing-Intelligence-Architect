import asyncio
from pathlib import Path

import pytest
from sqlmodel import Session, select

from marketing_intelligence.crawl_history import (
    recover_interrupted_runs,
    run_crawl,
    start_crawl_run,
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
from marketing_intelligence.models import CrawlPageRecord, CrawlRun, Site


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

    async def crawl(self, start_url: str, settings: CrawlSettings) -> CrawlResult:
        current = saved_runs(self.engine)
        self.saw_running = (
            len(current) == 1
            and current[0].status == "running"
            and current[0].completed_at is None
            and current[0].processed is None
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

        async def crawl(self, start_url: str, settings: CrawlSettings) -> CrawlResult:
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


def test_unexpected_exception_is_saved_as_failed_and_reraised(tmp_path: Path) -> None:
    engine = initialized_engine(tmp_path)

    class FailingCrawler:
        async def crawl(self, start_url: str, settings: CrawlSettings) -> CrawlResult:
            raise RuntimeError("сбой транспорта")

    with pytest.raises(RuntimeError, match="сбой транспорта"):
        asyncio.run(
            run_crawl(engine, 1, "https://example.com/", crawler=FailingCrawler())
        )

    run = saved_runs(engine)[0]
    assert run.status == "failed"
    assert run.completed_at is not None
    assert "сбой транспорта" in run.message
    assert run.processed is None


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
    assert recovered.processed is None
    assert recovered.limited is None
    restarted.dispose()
