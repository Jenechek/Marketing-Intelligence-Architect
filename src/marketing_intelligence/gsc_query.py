"""Read-only запросы истории и показателей Search Console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import CrawlPageRecord, CrawlRun, GSCImport, GSCPageMetric


ITEMS_PER_PAGE = 20
CRAWLER_STATUS_PRESENT = "Есть в последнем обходе"
CRAWLER_STATUS_MISSING = "Не найдена в последнем обходе"
CRAWLER_STATUS_UNAVAILABLE = "Подходящего обхода пока нет"


@dataclass(frozen=True)
class PageSlice:
    items: tuple
    page: int
    total_pages: int
    total_items: int


@dataclass(frozen=True)
class MetricView:
    metric: GSCPageMetric
    ctr: Decimal
    crawler_status: str


def list_imports(engine: Engine, site_id: int, page: int) -> PageSlice:
    with Session(engine) as session:
        total = session.exec(
            select(func.count()).select_from(GSCImport).where(GSCImport.site_id == site_id)
        ).one()
        total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        rows = session.exec(
            select(GSCImport)
            .where(GSCImport.site_id == site_id)
            .order_by(GSCImport.imported_at.desc(), GSCImport.id.desc())
            .offset((page - 1) * ITEMS_PER_PAGE)
            .limit(ITEMS_PER_PAGE)
        ).all()
        return PageSlice(tuple(rows), page, total_pages, total)


def list_periods(engine: Engine, site_id: int) -> tuple[tuple[date, date], ...]:
    with Session(engine) as session:
        rows = session.exec(
            select(GSCPageMetric.period_start, GSCPageMetric.period_end)
            .where(GSCPageMetric.site_id == site_id)
            .distinct()
            .order_by(GSCPageMetric.period_end.desc(), GSCPageMetric.period_start.desc())
        ).all()
        return tuple((row[0], row[1]) for row in rows)


def list_metrics(
    engine: Engine,
    site_id: int,
    period_start: date,
    period_end: date,
    page: int,
) -> PageSlice:
    """Загрузить страницу показателей и crawler-состояния фиксированным числом запросов."""

    with Session(engine) as session:
        conditions = (
            GSCPageMetric.site_id == site_id,
            GSCPageMetric.period_start == period_start,
            GSCPageMetric.period_end == period_end,
        )
        total = session.exec(
            select(func.count()).select_from(GSCPageMetric).where(*conditions)
        ).one()
        total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        metrics = session.exec(
            select(GSCPageMetric)
            .where(*conditions)
            .order_by(GSCPageMetric.normalized_url.asc(), GSCPageMetric.id.asc())
            .offset((page - 1) * ITEMS_PER_PAGE)
            .limit(ITEMS_PER_PAGE)
        ).all()
        run = session.exec(
            select(CrawlRun)
            .where(
                CrawlRun.site_id == site_id,
                CrawlRun.status.in_(("completed", "partial")),
                CrawlRun.completed_at.is_not(None),
            )
            .order_by(CrawlRun.completed_at.desc(), CrawlRun.id.desc())
            .limit(1)
        ).first()
        crawled_urls: set[str] | None = None
        if run is not None and run.id is not None:
            crawled_urls = set(
                session.exec(
                    select(CrawlPageRecord.url).where(CrawlPageRecord.crawl_run_id == run.id)
                ).all()
            )
        views = tuple(
            MetricView(
                metric=metric,
                ctr=metric.ctr,
                crawler_status=(
                    CRAWLER_STATUS_UNAVAILABLE
                    if crawled_urls is None
                    else CRAWLER_STATUS_PRESENT
                    if metric.normalized_url in crawled_urls
                    else CRAWLER_STATUS_MISSING
                ),
            )
            for metric in metrics
        )
        return PageSlice(views, page, total_pages, total)


def count_import_data(engine: Engine, site_id: int) -> tuple[int, int]:
    with Session(engine) as session:
        imports = session.exec(
            select(func.count()).select_from(GSCImport).where(GSCImport.site_id == site_id)
        ).one()
        metrics = session.exec(
            select(func.count()).select_from(GSCPageMetric).where(GSCPageMetric.site_id == site_id)
        ).one()
        return imports, metrics
