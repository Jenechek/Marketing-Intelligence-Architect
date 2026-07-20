"""Ограниченная загрузка карты структуры из слоя хранения."""

from dataclasses import dataclass
import json

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import CrawlPageRecord, CrawlPageSnapshot, CrawlRun
from .site_structure import RawStructurePage, SiteStructure, StructureDataError, build_site_structure


ELIGIBLE_STATUSES = ("completed", "partial")


@dataclass(frozen=True, slots=True)
class SelectedSiteStructure:
    run: CrawlRun
    structure: SiteStructure


def has_site_structure(engine: Engine, site_id: int) -> bool:
    """Проверить наличие пригодного запуска без загрузки страниц."""

    with Session(engine) as session:
        return session.exec(
            select(CrawlRun.id)
            .where(
                CrawlRun.site_id == site_id,
                CrawlRun.status.in_(ELIGIBLE_STATUSES),
                CrawlRun.completed_at.is_not(None),
            )
            .limit(1)
        ).first() is not None


def load_site_structure(engine: Engine, site_id: int) -> SelectedSiteStructure | None:
    """Двумя запросами получить новый пригодный запуск и все его страницы."""

    with Session(engine) as session:
        run = session.exec(
            select(CrawlRun)
            .where(CrawlRun.site_id == site_id, CrawlRun.status.in_(ELIGIBLE_STATUSES))
            .order_by(CrawlRun.completed_at.desc(), CrawlRun.id.desc())
            .limit(1)
        ).first()
        if run is None:
            return None
        if run.id is None or run.completed_at is None:
            raise StructureDataError("У выбранного обхода отсутствуют обязательные метаданные.")
        rows = session.exec(
            select(CrawlPageRecord, CrawlPageSnapshot)
            .outerjoin(
                CrawlPageSnapshot,
                CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
            )
            .where(CrawlPageRecord.crawl_run_id == run.id)
            .order_by(CrawlPageRecord.sequence_number, CrawlPageRecord.id)
        ).all()
        raw_pages = tuple(_raw_page(record, snapshot) for record, snapshot in rows)
        return SelectedSiteStructure(run, build_site_structure(raw_pages))


def _raw_page(
    record: CrawlPageRecord, snapshot: CrawlPageSnapshot | None
) -> RawStructurePage:
    links: tuple[str, ...] | None = None
    if snapshot is not None:
        try:
            decoded = json.loads(snapshot.internal_links_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise StructureDataError("Список внутренних ссылок нельзя прочитать.") from error
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise StructureDataError("Список внутренних ссылок имеет неверный формат.")
        links = tuple(decoded)
    if record.id is None:
        raise StructureDataError("Запись страницы не имеет идентификатора.")
    return RawStructurePage(
        record.id, record.sequence_number, record.url, record.depth, record.outcome,
        record.message, record.http_status, links,
    )
