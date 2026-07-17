"""Read-only загрузка и фильтрация сохранённых событий изменения."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .change_event import ChangeEventType
from .change_importance import ChangeImportance
from .models import CrawlRun, SnapshotChangeEvent


@dataclass(frozen=True, slots=True)
class ChangeEventItem:
    """Неизменяемое представление одного сохранённого события."""

    event_id: int
    event_type: ChangeEventType
    url: str
    current_completed_at: datetime
    importance: ChangeImportance
    weight: int
    current_run_id: int
    previous_run_id: int
    current_page_record_id: int | None
    previous_page_record_id: int | None
    text_distance: int | None
    change_ratio: Fraction | None


@dataclass(frozen=True, slots=True)
class ChangeEventPage:
    """Неизменяемая страница отфильтрованных событий."""

    items: tuple[ChangeEventItem, ...]
    total_count: int
    limit: int
    offset: int


def load_change_events(
    engine: Engine,
    *,
    site_id: int,
    event_types: Collection[ChangeEventType | str] | None = None,
    importance_levels: Collection[ChangeImportance | str] | None = None,
    from_time: datetime | None = None,
    before_time: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ChangeEventPage:
    """Загрузить страницу событий сайта по сочетаемым фильтрам."""

    normalized_from = _normalize_datetime(from_time, "from_time")
    normalized_before = _normalize_datetime(before_time, "before_time")
    _validate_pagination(limit, offset)
    if (
        normalized_from is not None
        and normalized_before is not None
        and normalized_from >= normalized_before
    ):
        raise ValueError("from_time должно быть раньше before_time.")

    type_values = _enum_values(event_types, ChangeEventType, "event_types")
    importance_values = _enum_values(
        importance_levels,
        ChangeImportance,
        "importance_levels",
    )
    filters = [CrawlRun.site_id == site_id]
    if type_values is not None:
        filters.append(SnapshotChangeEvent.event_type.in_(type_values))
    if importance_values is not None:
        filters.append(SnapshotChangeEvent.importance.in_(importance_values))
    if normalized_from is not None:
        filters.append(
            SnapshotChangeEvent.current_completed_at >= normalized_from
        )
    if normalized_before is not None:
        filters.append(
            SnapshotChangeEvent.current_completed_at < normalized_before
        )

    count_statement = (
        select(func.count(SnapshotChangeEvent.id))
        .select_from(SnapshotChangeEvent)
        .join(CrawlRun, SnapshotChangeEvent.current_run_id == CrawlRun.id)
        .where(*filters)
    )
    item_statement = (
        select(
            SnapshotChangeEvent.id,
            SnapshotChangeEvent.event_type,
            SnapshotChangeEvent.url,
            SnapshotChangeEvent.current_completed_at,
            SnapshotChangeEvent.importance,
            SnapshotChangeEvent.weight,
            SnapshotChangeEvent.current_run_id,
            SnapshotChangeEvent.previous_run_id,
            SnapshotChangeEvent.current_page_record_id,
            SnapshotChangeEvent.previous_page_record_id,
            SnapshotChangeEvent.text_distance,
            SnapshotChangeEvent.change_ratio_numerator,
            SnapshotChangeEvent.change_ratio_denominator,
        )
        .join(CrawlRun, SnapshotChangeEvent.current_run_id == CrawlRun.id)
        .where(*filters)
        .order_by(
            SnapshotChangeEvent.current_completed_at.desc(),
            SnapshotChangeEvent.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )

    with Session(engine) as session:
        total_count = session.exec(count_statement).one()
        rows = session.exec(item_statement).all()

    return ChangeEventPage(
        items=tuple(_item_from_row(row) for row in rows),
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


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


def _enum_values(values, enum_type, name: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    try:
        return tuple(enum_type(value).value for value in values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} содержит неподдерживаемое значение.") from error


def _item_from_row(row) -> ChangeEventItem:
    numerator = row.change_ratio_numerator
    denominator = row.change_ratio_denominator
    if (numerator is None) != (denominator is None):
        raise ValueError(
            f"У события {row.id} повреждена точная доля изменения."
        )
    ratio = (
        None
        if numerator is None
        else Fraction(numerator, denominator)
    )
    return ChangeEventItem(
        event_id=row.id,
        event_type=ChangeEventType(row.event_type),
        url=row.url,
        current_completed_at=row.current_completed_at,
        importance=ChangeImportance(row.importance),
        weight=row.weight,
        current_run_id=row.current_run_id,
        previous_run_id=row.previous_run_id,
        current_page_record_id=row.current_page_record_id,
        previous_page_record_id=row.previous_page_record_id,
        text_distance=row.text_distance,
        change_ratio=ratio,
    )
