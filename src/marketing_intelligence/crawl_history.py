"""Транзакционное хранение запусков обхода и метаданных страниц."""

from datetime import UTC, datetime
import json

from sqlalchemy import func, text, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .crawler import (
    CrawlCounters,
    CrawlResult,
    CrawlSettings,
    CrawlStatus,
    Crawler,
    PageOutcome,
)
from .models import CrawlPageRecord, CrawlPageSnapshot, CrawlRun, Site


RUNNING_STATUS = "running"
COMPLETED_STATUS = "completed"
PARTIAL_STATUS = "partial"
DEFERRED_STATUS = "deferred"
FAILED_STATUS = "failed"
INTERRUPTED_STATUS = "interrupted"

STATUS_TITLES = {
    RUNNING_STATUS: "Выполняется",
    COMPLETED_STATUS: "Завершён",
    PARTIAL_STATUS: "Завершён частично",
    DEFERRED_STATUS: "Отложен",
    FAILED_STATUS: "Ошибка",
    INTERRUPTED_STATUS: "Прерван",
}

ERROR_OUTCOME_TITLES = {
    PageOutcome.OVERSIZED.value: "Слишком большая HTML-страница",
    PageOutcome.PARSE_ERROR.value: "Ошибка разбора HTML",
    PageOutcome.REDIRECT.value: "Перенаправление",
    PageOutcome.HTTP_ERROR.value: "Ошибка HTTP",
    PageOutcome.NETWORK_ERROR.value: "Сетевая ошибка",
    PageOutcome.TIMEOUT.value: "Истекло время ожидания",
}


def crawl_status_title(status: str) -> str:
    """Вернуть понятное русское название сохранённого статуса."""

    return STATUS_TITLES.get(status, "Неизвестный статус")


def crawl_error_outcome_title(outcome: str) -> str:
    """Вернуть понятное русское название ошибки страницы."""

    return ERROR_OUTCOME_TITLES.get(outcome, "Неизвестная ошибка страницы")


class ActiveCrawlRunError(RuntimeError):
    """Новый запуск отклонён, потому что другой обход уже выполняется."""

    def __init__(self, run_id: int) -> None:
        super().__init__("Полный обход уже выполняется.")
        self.run_id = run_id


def start_crawl_run(
    engine: Engine,
    site_id: int,
    settings: CrawlSettings,
) -> CrawlRun:
    """Зафиксировать запуск до обращения к crawler."""

    with Session(engine, expire_on_commit=False) as session:
        session.exec(text("BEGIN IMMEDIATE"))
        if session.get(Site, site_id) is None:
            raise LookupError("Сайт для запуска обхода не найден.")

        active_run = session.exec(
            select(CrawlRun)
            .where(CrawlRun.status == RUNNING_STATUS)
            .order_by(CrawlRun.started_at, CrawlRun.id)
        ).first()
        if active_run is not None:
            active_run_id = active_run.id
            session.rollback()
            if active_run_id is None:
                raise LookupError("Активный запуск обхода не имеет идентификатора.")
            raise ActiveCrawlRunError(active_run_id)

        run = CrawlRun(
            site_id=site_id,
            status=RUNNING_STATUS,
            message="Обход выполняется.",
            max_pages=settings.max_pages,
            max_depth=settings.max_depth,
            delay=settings.delay,
            timeout=settings.timeout,
            user_agent=settings.user_agent,
            processed=0,
            requested=0,
            successful=0,
            forbidden=0,
            errors=0,
        )
        session.add(run)
        session.commit()
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
    return await execute_crawl_run(
        engine,
        run.id,
        start_url,
        crawler=crawler,
        settings=active_settings,
    )


async def execute_crawl_run(
    engine: Engine,
    run_id: int | None,
    start_url: str,
    *,
    crawler: Crawler | None = None,
    settings: CrawlSettings | None = None,
) -> CrawlRun:
    """Выполнить заранее сохранённый запуск и атомарно записать его итог."""

    if run_id is None:
        raise LookupError("Начатый запуск обхода не имеет идентификатора.")
    active_settings = settings or CrawlSettings()
    active_crawler = crawler or Crawler()

    async def save_progress(counters: CrawlCounters) -> None:
        update_crawl_progress(engine, run_id, counters)

    try:
        result = await active_crawler.crawl(
            start_url,
            active_settings,
            progress=save_progress,
        )
        return _complete_crawl_run(engine, run_id, result)
    except Exception as error:
        _fail_crawl_run(engine, run_id, error)
        raise


def update_crawl_progress(
    engine: Engine,
    run_id: int,
    counters: CrawlCounters,
) -> None:
    """Сохранить только текущие счётчики, не публикуя записи страниц."""

    with Session(engine) as session:
        run = session.get(CrawlRun, run_id)
        if run is None or run.status != RUNNING_STATUS:
            return
        run.processed = counters.processed
        run.requested = counters.requested
        run.successful = counters.successful
        run.forbidden = counters.forbidden
        run.errors = counters.errors
        session.add(run)
        session.commit()


def get_crawl_run(engine: Engine, run_id: int) -> CrawlRun | None:
    """Получить запуск по идентификатору."""

    with Session(engine) as session:
        return session.get(CrawlRun, run_id)


def list_crawl_errors(engine: Engine, run_id: int) -> list[CrawlPageRecord]:
    """Вернуть сохранённые ошибки страниц в порядке обработки."""

    with Session(engine) as session:
        return list(
            session.exec(
                select(CrawlPageRecord)
                .where(
                    CrawlPageRecord.crawl_run_id == run_id,
                    CrawlPageRecord.outcome.in_(ERROR_OUTCOME_TITLES),
                )
                .order_by(CrawlPageRecord.sequence_number)
            ).all()
        )


def get_running_crawl_run(engine: Engine, site_id: int) -> CrawlRun | None:
    """Получить сохранённый активный запуск выбранного сайта."""

    with Session(engine) as session:
        return session.exec(
            select(CrawlRun)
            .where(
                CrawlRun.site_id == site_id,
                CrawlRun.status == RUNNING_STATUS,
            )
            .order_by(CrawlRun.started_at, CrawlRun.id)
        ).first()


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


def count_crawl_data(engine: Engine, site_id: int) -> tuple[int, int, int]:
    """Вернуть количества запусков, страниц и снимков выбранного сайта."""

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
        snapshots = session.exec(
            select(func.count())
            .select_from(CrawlPageSnapshot)
            .join(
                CrawlPageRecord,
                CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
            )
            .join(CrawlRun, CrawlPageRecord.crawl_run_id == CrawlRun.id)
            .where(CrawlRun.site_id == site_id)
        ).one()
        return runs, pages, snapshots


def _complete_crawl_run(
    engine: Engine,
    run_id: int | None,
    result: CrawlResult,
) -> CrawlRun:
    if run_id is None:
        raise LookupError("Начатый запуск обхода не имеет идентификатора.")

    with Session(engine, expire_on_commit=False) as session:
        run = session.get(CrawlRun, run_id)
        if run is None:
            raise LookupError("Начатый запуск обхода не найден.")

        stored_status = _stored_status(result)
        run.completed_at = datetime.now(UTC)
        run.status = stored_status
        run.message = result.message
        run.robots_status = result.robots_status
        run.processed = result.counters.processed
        run.requested = result.counters.requested
        run.successful = result.counters.successful
        run.forbidden = result.counters.forbidden
        run.errors = result.counters.errors
        run.limited = result.limited
        session.add(run)
        if stored_status in {COMPLETED_STATUS, PARTIAL_STATUS}:
            for sequence_number, page in enumerate(result.pages, start=1):
                record = CrawlPageRecord(
                    crawl_run_id=run_id,
                    sequence_number=sequence_number,
                    url=page.url,
                    depth=page.depth,
                    outcome=page.outcome.value,
                    message=page.message,
                    http_status=page.http_status,
                )
                session.add(record)
                session.flush()
                if page.outcome is PageOutcome.HTML:
                    if page.page_data is None:
                        raise ValueError(
                            "Успешная HTML-страница не содержит извлечённых данных."
                        )
                    if record.id is None:
                        raise LookupError("Запись страницы не получила идентификатор.")
                    data = page.page_data
                    session.add(
                        CrawlPageSnapshot(
                            crawl_page_record_id=record.id,
                            checked_at=data.checked_at,
                            title=data.title,
                            description=data.description,
                            h1=data.h1,
                            normalized_text=data.normalized_text,
                            content_hash=data.content_hash,
                            internal_links_json=_encode_internal_links(
                                data.internal_links
                            ),
                        )
                    )
        session.commit()
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


def _encode_internal_links(links: tuple[str, ...]) -> str:
    """Канонически кодировать упорядоченные ссылки переносимым JSON-текстом."""

    return json.dumps(links, ensure_ascii=False, separators=(",", ":"))
