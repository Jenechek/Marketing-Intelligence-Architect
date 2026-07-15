"""Транзакционное хранение запусков обхода и метаданных страниц."""

from datetime import UTC, datetime

from sqlalchemy import func, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .crawler import CrawlResult, CrawlSettings, CrawlStatus, Crawler
from .models import CrawlPageRecord, CrawlRun


RUNNING_STATUS = "running"
COMPLETED_STATUS = "completed"
PARTIAL_STATUS = "partial"
DEFERRED_STATUS = "deferred"
FAILED_STATUS = "failed"
INTERRUPTED_STATUS = "interrupted"


def start_crawl_run(
    engine: Engine,
    site_id: int,
    settings: CrawlSettings,
) -> CrawlRun:
    """Зафиксировать запуск до обращения к crawler."""

    run = CrawlRun(
        site_id=site_id,
        status=RUNNING_STATUS,
        message="Обход выполняется.",
        max_pages=settings.max_pages,
        max_depth=settings.max_depth,
        delay=settings.delay,
        timeout=settings.timeout,
        user_agent=settings.user_agent,
    )
    with Session(engine) as session:
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


async def run_crawl(
    engine: Engine,
    site_id: int,
    start_url: str,
    *,
    crawler: Crawler | None = None,
    settings: CrawlSettings | None = None,
) -> CrawlRun:
    """Выполнить обход, сохранив начало и атомарно — его итог."""

    active_settings = settings or CrawlSettings()
    run = start_crawl_run(engine, site_id, active_settings)
    active_crawler = crawler or Crawler()
    try:
        result = await active_crawler.crawl(start_url, active_settings)
    except Exception as error:
        _fail_crawl_run(engine, run.id, error)
        raise
    return _complete_crawl_run(engine, run.id, result)


def recover_interrupted_runs(engine: Engine) -> int:
    """Пометить незавершённые запуски прошлого процесса как прерванные."""

    with Session(engine) as session:
        statement = (
            update(CrawlRun)
            .where(CrawlRun.status == RUNNING_STATUS)
            .values(
                status=INTERRUPTED_STATUS,
                message="Обход был прерван перезапуском приложения.",
                completed_at=datetime.now(UTC),
            )
        )
        result = session.exec(statement)
        session.commit()
        return result.rowcount or 0


def count_crawl_data(engine: Engine, site_id: int) -> tuple[int, int]:
    """Вернуть отдельные количества запусков и записей страниц сайта."""

    with Session(engine) as session:
        runs = session.exec(
            select(func.count())
            .select_from(CrawlRun)
            .where(CrawlRun.site_id == site_id)
        ).one()
        pages = session.exec(
            select(func.count())
            .select_from(CrawlPageRecord)
            .join(CrawlRun, CrawlPageRecord.crawl_run_id == CrawlRun.id)
            .where(CrawlRun.site_id == site_id)
        ).one()
        return runs, pages


def _complete_crawl_run(
    engine: Engine,
    run_id: int | None,
    result: CrawlResult,
) -> CrawlRun:
    if run_id is None:
        raise LookupError("Начатый запуск обхода не имеет идентификатора.")

    with Session(engine) as session:
        run = session.get(CrawlRun, run_id)
        if run is None:
            raise LookupError("Начатый запуск обхода не найден.")

        run.completed_at = datetime.now(UTC)
        run.status = _stored_status(result)
        run.message = result.message
        run.robots_status = result.robots_status
        run.processed = result.counters.processed
        run.requested = result.counters.requested
        run.successful = result.counters.successful
        run.forbidden = result.counters.forbidden
        run.errors = result.counters.errors
        run.limited = result.limited
        session.add(run)
        session.add_all(
            CrawlPageRecord(
                crawl_run_id=run_id,
                sequence_number=sequence_number,
                url=page.url,
                depth=page.depth,
                outcome=page.outcome.value,
                message=page.message,
                http_status=page.http_status,
            )
            for sequence_number, page in enumerate(result.pages, start=1)
        )
        session.commit()
        session.refresh(run)
        return run


def _fail_crawl_run(engine: Engine, run_id: int | None, error: Exception) -> None:
    if run_id is None:
        raise LookupError("Начатый запуск обхода не имеет идентификатора.") from error
    with Session(engine) as session:
        run = session.get(CrawlRun, run_id)
        if run is None:
            raise LookupError("Начатый запуск обхода не найден.") from error
        run.completed_at = datetime.now(UTC)
        run.status = FAILED_STATUS
        run.message = f"Обход завершился неожиданной ошибкой: {error}"
        session.add(run)
        session.commit()


def _stored_status(result: CrawlResult) -> str:
    if result.status is not CrawlStatus.COMPLETED:
        return DEFERRED_STATUS
    if result.counters.errors:
        return PARTIAL_STATUS
    return COMPLETED_STATUS
