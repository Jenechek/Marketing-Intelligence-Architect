"""Read-only объединённая загрузка обычных и ценовых событий."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from sqlalchemy import String, cast, func, literal, union_all
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .change_event import ChangeEventType, HistoryEventType, PriceChangeEventType
from .change_importance import ChangeImportance
from .models import CrawlRun, PriceChangeEvent, Site, SnapshotChangeEvent


@dataclass(frozen=True, slots=True)
class ChangeEventItem:
    event_id: int
    source: str
    source_rank: int
    site_id: int
    site_name: str
    site_url: str
    event_type: HistoryEventType
    url: str
    current_completed_at: datetime
    importance: ChangeImportance | None
    weight: int | None
    current_run_id: int
    previous_run_id: int
    current_page_record_id: int | None
    previous_page_record_id: int | None
    text_distance: int | None
    change_ratio: Fraction | None


@dataclass(frozen=True, slots=True)
class ChangeEventPage:
    items: tuple[ChangeEventItem, ...]
    total_count: int
    limit: int
    offset: int


def load_change_events(
    engine: Engine,
    *,
    site_id: int | None = None,
    event_types: Collection[HistoryEventType | str] | None = None,
    importance_levels: Collection[ChangeImportance | str] | None = None,
    from_time: datetime | None = None,
    before_time: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ChangeEventPage:
    """Выполнить один общий count и один объединённый select без N+1."""

    normalized_from = _normalize_datetime(from_time, "from_time")
    normalized_before = _normalize_datetime(before_time, "before_time")
    _validate_pagination(limit, offset)
    if normalized_from is not None and normalized_before is not None and normalized_from >= normalized_before:
        raise ValueError("from_time должно быть раньше before_time.")
    if site_id is not None and (isinstance(site_id, bool) or not isinstance(site_id, int) or site_id < 1):
        raise ValueError("site_id должен быть положительным целым числом.")
    type_values = _history_type_values(event_types)
    importance_values = _enum_values(importance_levels, ChangeImportance, "importance_levels")

    snapshot = (
        select(
            SnapshotChangeEvent.id.label("event_id"),
            literal("snapshot").label("source"),
            literal(0).label("source_rank"),
            Site.id.label("site_id"), Site.name.label("site_name"), Site.url.label("site_url"),
            SnapshotChangeEvent.event_type.label("event_type"), SnapshotChangeEvent.url.label("url"),
            SnapshotChangeEvent.current_completed_at.label("current_completed_at"),
            SnapshotChangeEvent.importance.label("importance"), SnapshotChangeEvent.weight.label("weight"),
            SnapshotChangeEvent.current_run_id.label("current_run_id"),
            SnapshotChangeEvent.previous_run_id.label("previous_run_id"),
            SnapshotChangeEvent.current_page_record_id.label("current_page_record_id"),
            SnapshotChangeEvent.previous_page_record_id.label("previous_page_record_id"),
            SnapshotChangeEvent.text_distance.label("text_distance"),
            SnapshotChangeEvent.change_ratio_numerator.label("change_ratio_numerator"),
            SnapshotChangeEvent.change_ratio_denominator.label("change_ratio_denominator"),
        )
        .join(CrawlRun, SnapshotChangeEvent.current_run_id == CrawlRun.id)
        .join(Site, CrawlRun.site_id == Site.id)
    )
    price = (
        select(
            PriceChangeEvent.id.label("event_id"),
            literal("price").label("source"),
            literal(1).label("source_rank"),
            Site.id.label("site_id"), Site.name.label("site_name"), Site.url.label("site_url"),
            literal(PriceChangeEventType.PRICE_CHANGED.value).label("event_type"),
            PriceChangeEvent.url.label("url"), PriceChangeEvent.current_completed_at.label("current_completed_at"),
            cast(literal(None), String).label("importance"),
            literal(None).label("weight"),
            PriceChangeEvent.current_run_id.label("current_run_id"),
            PriceChangeEvent.previous_run_id.label("previous_run_id"),
            PriceChangeEvent.current_page_record_id.label("current_page_record_id"),
            PriceChangeEvent.previous_page_record_id.label("previous_page_record_id"),
            literal(None).label("text_distance"), literal(None).label("change_ratio_numerator"),
            literal(None).label("change_ratio_denominator"),
        )
        .join(CrawlRun, PriceChangeEvent.current_run_id == CrawlRun.id)
        .join(Site, CrawlRun.site_id == Site.id)
    )
    combined = union_all(snapshot, price).subquery("combined_change_events")
    filters = []
    if site_id is not None:
        filters.append(combined.c.site_id == site_id)
    if type_values is not None:
        filters.append(combined.c.event_type.in_(type_values))
    if importance_values is not None:
        filters.append(combined.c.importance.in_(importance_values))
    if normalized_from is not None:
        filters.append(combined.c.current_completed_at >= normalized_from)
    if normalized_before is not None:
        filters.append(combined.c.current_completed_at < normalized_before)
    count_statement = select(func.count()).select_from(combined).where(*filters)
    item_statement = (
        select(*combined.c)
        .where(*filters)
        .order_by(
            combined.c.current_completed_at.desc(),
            combined.c.source_rank.asc(),
            combined.c.event_id.desc(),
        )
        .limit(limit).offset(offset)
    )
    with Session(engine) as session:
        total_count = session.exec(count_statement).one()
        rows = session.exec(item_statement).all()
    return ChangeEventPage(tuple(_item_from_row(row) for row in rows), total_count, limit, offset)


def _normalize_datetime(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} должно содержать часовой пояс.")
    return value.astimezone(UTC)


def _validate_pagination(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit должен быть целым числом от 1 до 200.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset должен быть целым неотрицательным числом.")


def _history_type_values(values) -> tuple[str, ...] | None:
    if values is None:
        return None
    allowed = {item.value for item in ChangeEventType} | {PriceChangeEventType.PRICE_CHANGED.value}
    result = tuple(value.value if hasattr(value, "value") else value for value in values)
    if any(value not in allowed for value in result):
        raise ValueError("event_types содержит неподдерживаемое значение.")
    return result


def _enum_values(values, enum_type, name: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    try:
        return tuple(enum_type(value).value for value in values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} содержит неподдерживаемое значение.") from error


def _item_from_row(row) -> ChangeEventItem:
    numerator, denominator = row.change_ratio_numerator, row.change_ratio_denominator
    if (numerator is None) != (denominator is None):
        raise ValueError(f"У события {row.event_id} повреждена точная доля изменения.")
    event_type = (
        PriceChangeEventType.PRICE_CHANGED
        if row.event_type == PriceChangeEventType.PRICE_CHANGED.value
        else ChangeEventType(row.event_type)
    )
    return ChangeEventItem(
        event_id=row.event_id, source=row.source, source_rank=row.source_rank,
        site_id=row.site_id, site_name=row.site_name, site_url=row.site_url,
        event_type=event_type, url=row.url, current_completed_at=row.current_completed_at,
        importance=ChangeImportance(row.importance) if row.importance is not None else None,
        weight=row.weight, current_run_id=row.current_run_id, previous_run_id=row.previous_run_id,
        current_page_record_id=row.current_page_record_id,
        previous_page_record_id=row.previous_page_record_id,
        text_distance=row.text_distance,
        change_ratio=None if numerator is None else Fraction(numerator, denominator),
    )
