"""Правила и операции управления сайтами."""

from urllib.parse import urlsplit

from sqlalchemy import delete, exists, or_, text, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import (
    AvailabilityCheck,
    ChangeEventViewState,
    CrawlPagePriceRecord,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    GSCImport,
    GSCPageMetric,
    PriceChangeEvent,
    Site,
    SITE_TYPE_COMPETITOR,
    SITE_TYPE_OWNED,
    SITE_TYPES,
    SnapshotChangeEvent,
)
from .scheduler import delete_schedule_data


class ActiveSiteCrawlError(RuntimeError):
    """Удаление сайта запрещено, пока его полный обход выполняется."""

    def __init__(self, run_id: int) -> None:
        super().__init__("Нельзя удалить сайт во время полного обхода.")
        self.run_id = run_id


class SiteTransferBlockedError(RuntimeError):
    """Перенос собственного сайта заблокирован сохранёнными данными GSC."""


def validate_site_type(site_type: str) -> str:
    """Проверить доменный тип сайта."""

    if site_type not in SITE_TYPES:
        raise ValueError("Неизвестный тип сайта.")
    return site_type


def validate_site(name: str, url: str) -> dict[str, str]:
    """Проверить базовые данные сайта и вернуть понятные ошибки формы."""

    errors: dict[str, str] = {}
    clean_name = name.strip()
    clean_url = url.strip()

    if not clean_name:
        errors["name"] = "Укажите понятное название сайта."

    if not clean_url:
        errors["url"] = "Укажите URL сайта."
        return errors

    if any(character.isspace() for character in clean_url):
        errors["url"] = "URL не должен содержать пробелы."
        return errors

    try:
        parsed_url = urlsplit(clean_url)
        _ = parsed_url.port
    except ValueError:
        errors["url"] = "Проверьте адрес и номер порта."
        return errors

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        errors["url"] = "Введите полный URL, например https://example.com."
    elif parsed_url.username or parsed_url.password:
        errors["url"] = "Не указывайте логин или пароль в URL."

    return errors


def list_sites(engine: Engine, site_type: str | None = None) -> list[Site]:
    """Вернуть добавленные сайты, начиная с последнего."""

    with Session(engine) as session:
        statement = select(Site)
        if site_type is not None:
            statement = statement.where(Site.site_type == validate_site_type(site_type))
        statement = statement.order_by(Site.created_at.desc(), Site.id.desc())
        return list(session.exec(statement).all())


def get_site(engine: Engine, site_id: int) -> Site | None:
    """Вернуть сайт по идентификатору или ``None``, если сайт не найден."""

    with Session(engine) as session:
        return session.get(Site, site_id)


def get_site_of_type(engine: Engine, site_id: int, site_type: str) -> Site | None:
    """Вернуть сайт только из ожидаемого пользовательского инструмента."""

    expected = validate_site_type(site_type)
    with Session(engine) as session:
        return session.exec(
            select(Site).where(Site.id == site_id, Site.site_type == expected)
        ).one_or_none()


def add_site(
    engine: Engine,
    name: str,
    url: str,
    site_type: str = SITE_TYPE_COMPETITOR,
) -> Site:
    """Сохранить проверенные данные сайта одной транзакцией."""

    site = Site(
        name=name.strip(),
        url=url.strip(),
        site_type=validate_site_type(site_type),
    )
    with Session(engine) as session:
        session.add(site)
        session.commit()
        session.refresh(site)
        return site


def transfer_site(
    engine: Engine,
    site_id: int,
    source_type: str,
    target_type: str,
) -> Site | None:
    """Атомарно перенести существующий сайт без изменения связанных данных."""

    source = validate_site_type(source_type)
    target = validate_site_type(target_type)
    if source == target:
        raise ValueError("Исходный и целевой типы должны различаться.")
    if {source, target} != {SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED}:
        raise ValueError("Направление переноса не поддерживается.")

    with Session(engine) as session:
        statement = update(Site).where(Site.id == site_id, Site.site_type == source)
        if source == SITE_TYPE_OWNED:
            statement = statement.where(
                ~exists(select(GSCImport.id).where(GSCImport.site_id == site_id)),
                ~exists(select(GSCPageMetric.id).where(GSCPageMetric.site_id == site_id)),
            )
        result = session.exec(statement.values(site_type=target))
        if result.rowcount == 1:
            session.commit()
            return session.get(Site, site_id)

        current = session.get(Site, site_id)
        session.rollback()
        if (
            current is not None
            and current.site_type == source
            and source == SITE_TYPE_OWNED
        ):
            raise SiteTransferBlockedError(
                "Сайт нельзя перенести в конкуренты, пока у него есть "
                "импорты или показатели Search Console."
            )
        return None


def update_site(engine: Engine, site_id: int, name: str, url: str) -> Site | None:
    """Обновить проверенные данные сайта одной транзакцией."""

    with Session(engine) as session:
        site = session.get(Site, site_id)
        if site is None:
            return None

        site.name = name.strip()
        site.url = url.strip()
        session.add(site)
        session.commit()
        session.refresh(site)
        return site


def delete_site(engine: Engine, site_id: int) -> bool:
    """Окончательно удалить выбранный сайт и его историю одной транзакцией."""

    with Session(engine) as session:
        session.exec(text("BEGIN IMMEDIATE"))
        site = session.get(Site, site_id)
        if site is None:
            return False

        active_run_id = session.exec(
            select(CrawlRun.id).where(
                CrawlRun.site_id == site_id,
                CrawlRun.status == "running",
            )
        ).first()
        if active_run_id is not None:
            session.rollback()
            raise ActiveSiteCrawlError(active_run_id)

        session.exec(
            delete(AvailabilityCheck).where(AvailabilityCheck.site_id == site_id)
        )
        delete_schedule_data(session, site_id)
        run_ids = select(CrawlRun.id).where(CrawlRun.site_id == site_id)
        page_ids = select(CrawlPageRecord.id).where(
            CrawlPageRecord.crawl_run_id.in_(run_ids)
        )
        snapshot_event_ids = select(SnapshotChangeEvent.id).where(
            or_(
                SnapshotChangeEvent.current_run_id.in_(run_ids),
                SnapshotChangeEvent.previous_run_id.in_(run_ids),
            )
        )
        price_event_ids = select(PriceChangeEvent.id).where(
            or_(
                PriceChangeEvent.current_run_id.in_(run_ids),
                PriceChangeEvent.previous_run_id.in_(run_ids),
            )
        )
        session.exec(
            delete(ChangeEventViewState).where(
                or_(
                    ChangeEventViewState.snapshot_change_event_id.in_(snapshot_event_ids),
                    ChangeEventViewState.price_change_event_id.in_(price_event_ids),
                )
            )
        )
        session.exec(
            delete(PriceChangeEvent).where(
                or_(
                    PriceChangeEvent.current_run_id.in_(run_ids),
                    PriceChangeEvent.previous_run_id.in_(run_ids),
                )
            )
        )
        session.exec(
            delete(SnapshotChangeEvent).where(
                or_(
                    SnapshotChangeEvent.current_run_id.in_(run_ids),
                    SnapshotChangeEvent.previous_run_id.in_(run_ids),
                )
            )
        )
        session.exec(
            delete(CrawlPagePriceRecord).where(
                CrawlPagePriceRecord.crawl_page_snapshot_id.in_(page_ids)
            )
        )
        session.exec(
            delete(CrawlPageSnapshot).where(
                CrawlPageSnapshot.crawl_page_record_id.in_(page_ids)
            )
        )
        session.exec(
            delete(CrawlPageRecord).where(CrawlPageRecord.crawl_run_id.in_(run_ids))
        )
        session.exec(delete(CrawlRun).where(CrawlRun.site_id == site_id))
        session.exec(delete(GSCPageMetric).where(GSCPageMetric.site_id == site_id))
        session.exec(delete(GSCImport).where(GSCImport.site_id == site_id))
        session.delete(site)
        session.commit()
        return True
