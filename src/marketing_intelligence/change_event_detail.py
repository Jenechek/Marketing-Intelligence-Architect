"""Read-only загрузка точных значений отдельного события изменения."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .change_event import ChangeEventType, HistoryEventType, PriceChangeEventType
from .change_importance import ChangeImportance
from .models import (
    CrawlPageRecord,
    CrawlPagePriceRecord,
    CrawlPageSnapshot,
    CrawlRun,
    PriceChangeEvent,
    SnapshotChangeEvent,
)
from .snapshot_comparison_input import SnapshotPriceValue
from .snapshot_price_comparison import build_price_profile


class ChangeEventDataError(ValueError):
    """Сохранённое событие или связанный снимок повреждены."""


@dataclass(frozen=True, slots=True)
class SnapshotValues:
    """Точные значения существующей стороны события."""

    title: str | None
    description: str | None
    h1: str | None
    normalized_text: str
    internal_links: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PriceValues:
    """Точный однозначный ценовой профиль одной стороны."""

    profile: str
    currency: str
    low: str
    high: str | None


@dataclass(frozen=True, slots=True)
class ChangeEventDetail:
    """Неизменяемое событие: current/Стало всегда перед previous/Было."""

    event_id: int
    event_type: HistoryEventType
    url: str
    current_completed_at: datetime
    importance: ChangeImportance | None
    weight: int | None
    current_run_id: int
    previous_run_id: int
    current: SnapshotValues | PriceValues | None
    previous: SnapshotValues | PriceValues | None
    text_distance: int | None
    change_ratio: Fraction | None


def load_change_event(
    engine: Engine,
    *,
    site_id: int,
    event_id: int,
    source: str = "snapshot",
) -> ChangeEventDetail | None:
    """Загрузить одно событие сайта, не раскрывая события другого сайта."""

    if source == "price":
        return _load_price_change_event(engine, site_id=site_id, event_id=event_id)
    if source != "snapshot":
        raise ValueError("Неизвестный источник события.")

    with Session(engine) as session:
        row = session.exec(
            select(SnapshotChangeEvent, CrawlRun)
            .join(CrawlRun, SnapshotChangeEvent.current_run_id == CrawlRun.id)
            .where(
                SnapshotChangeEvent.id == event_id,
                CrawlRun.site_id == site_id,
            )
        ).first()
        if row is None:
            return None

        event, current_run = row
        previous_run = session.get(CrawlRun, event.previous_run_id)
        _validate_runs(session, event, current_run, previous_run, site_id)

        current = _load_side(
            session,
            event.current_page_record_id,
            event.current_run_id,
            event.url,
            "Стало",
        )
        previous = _load_side(
            session,
            event.previous_page_record_id,
            event.previous_run_id,
            event.url,
            "Было",
        )
        event_type = _event_type(event)
        _validate_sides(event.id, event_type, current, previous)
        ratio = _change_ratio(event)

    return ChangeEventDetail(
        event_id=event.id,
        event_type=event_type,
        url=event.url,
        current_completed_at=_as_utc(event.current_completed_at),
        importance=_importance(event),
        weight=event.weight,
        current_run_id=event.current_run_id,
        previous_run_id=event.previous_run_id,
        current=current,
        previous=previous,
        text_distance=event.text_distance,
        change_ratio=ratio,
    )


def _load_price_change_event(
    engine: Engine, *, site_id: int, event_id: int
) -> ChangeEventDetail | None:
    with Session(engine) as session:
        row = session.exec(
            select(PriceChangeEvent, CrawlRun)
            .join(CrawlRun, PriceChangeEvent.current_run_id == CrawlRun.id)
            .where(PriceChangeEvent.id == event_id, CrawlRun.site_id == site_id)
        ).first()
        if row is None:
            return None
        event, current_run = row
        previous_run = session.get(CrawlRun, event.previous_run_id)
        _validate_runs(session, event, current_run, previous_run, site_id)
        current = _load_price_side(
            session, event.current_page_record_id, event.current_run_id, event.url, "Стало"
        )
        previous = _load_price_side(
            session, event.previous_page_record_id, event.previous_run_id, event.url, "Было"
        )
        if (
            current.profile != event.profile
            or previous.profile != event.profile
            or current.currency != event.currency
            or previous.currency != event.currency
            or (current.low, current.high) == (previous.low, previous.high)
        ):
            raise ChangeEventDataError(
                f"Ценовые значения события {event.id} не соответствуют сохранённому профилю."
            )
    return ChangeEventDetail(
        event_id=event.id,
        event_type=PriceChangeEventType.PRICE_CHANGED,
        url=event.url,
        current_completed_at=_as_utc(event.current_completed_at),
        importance=None,
        weight=None,
        current_run_id=event.current_run_id,
        previous_run_id=event.previous_run_id,
        current=current,
        previous=previous,
        text_distance=None,
        change_ratio=None,
    )


def _load_price_side(
    session: Session,
    page_record_id: int,
    run_id: int,
    event_url: str,
    side_name: str,
) -> PriceValues:
    page = session.get(CrawlPageRecord, page_record_id)
    if page is None or page.crawl_run_id != run_id or page.url != event_url:
        raise ChangeEventDataError(
            f"Сторона «{side_name}» не соответствует обходу или URL события."
        )
    records = session.exec(
        select(CrawlPagePriceRecord)
        .where(CrawlPagePriceRecord.crawl_page_snapshot_id == page_record_id)
        .order_by(CrawlPagePriceRecord.sequence_number)
    ).all()
    values = []
    for record in records:
        try:
            amount = record.amount
        except (ArithmeticError, ValueError) as error:
            raise ChangeEventDataError(
                f"Цена стороны «{side_name}» повреждена."
            ) from error
        values.append(SnapshotPriceValue(amount, record.currency, record.kind, record.source))
    profile = build_price_profile(tuple(values))
    if profile is None:
        raise ChangeEventDataError(
            f"Цена стороны «{side_name}» больше не образует однозначный профиль."
        )
    return PriceValues(
        profile=profile.kind,
        currency=profile.currency,
        low=str(profile.low),
        high=str(profile.high) if profile.high is not None else None,
    )


def _validate_runs(
    session: Session,
    event: SnapshotChangeEvent | PriceChangeEvent,
    current: CrawlRun,
    previous: CrawlRun | None,
    site_id: int,
) -> None:
    if (
        previous is None
        or current.site_id != site_id
        or previous.site_id != site_id
        or current.status != "completed"
        or previous.status != "completed"
        or current.completed_at is None
        or previous.completed_at is None
        or _as_utc(current.completed_at) != _as_utc(event.current_completed_at)
    ):
        raise ChangeEventDataError(
            f"У события {event.id} повреждены ссылки на завершённые обходы."
        )

    expected_previous = session.exec(
        select(CrawlRun.id)
        .where(
            CrawlRun.site_id == site_id,
            CrawlRun.status == "completed",
            CrawlRun.completed_at.is_not(None),
            (CrawlRun.completed_at < current.completed_at)
            | (
                (CrawlRun.completed_at == current.completed_at)
                & (CrawlRun.id < current.id)
            ),
        )
        .order_by(CrawlRun.completed_at.desc(), CrawlRun.id.desc())
    ).first()
    if expected_previous != previous.id:
        raise ChangeEventDataError(
            f"У события {event.id} нарушена последовательность обходов."
        )


def _load_side(
    session: Session,
    page_record_id: int | None,
    run_id: int,
    event_url: str,
    side_name: str,
) -> SnapshotValues | None:
    if page_record_id is None:
        return None
    row = session.exec(
        select(CrawlPageRecord, CrawlPageSnapshot)
        .join(
            CrawlPageSnapshot,
            CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
        )
        .where(CrawlPageRecord.id == page_record_id)
    ).first()
    if row is None:
        raise ChangeEventDataError(
            f"Для стороны «{side_name}» не найден сохранённый снимок страницы."
        )
    page, snapshot = row
    if page.crawl_run_id != run_id or page.url != event_url:
        raise ChangeEventDataError(
            f"Сторона «{side_name}» не соответствует обходу или URL события."
        )
    return SnapshotValues(
        title=snapshot.title,
        description=snapshot.description,
        h1=snapshot.h1,
        normalized_text=snapshot.normalized_text,
        internal_links=_decode_links(snapshot.internal_links_json, page_record_id),
    )


def _decode_links(value: str, page_record_id: int) -> tuple[str, ...]:
    try:
        decoded: Any = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ChangeEventDataError(
            f"Внутренние ссылки снимка {page_record_id} содержат повреждённый JSON."
        ) from error
    if not isinstance(decoded, list) or not all(
        isinstance(link, str) for link in decoded
    ):
        raise ChangeEventDataError(
            f"Внутренние ссылки снимка {page_record_id} должны быть массивом строк."
        )
    return tuple(sorted(set(decoded)))


def _validate_sides(
    event_id: int,
    event_type: ChangeEventType,
    current: SnapshotValues | None,
    previous: SnapshotValues | None,
) -> None:
    valid = {
        ChangeEventType.PAGE_ADDED: current is not None and previous is None,
        ChangeEventType.PAGE_REMOVED: current is None and previous is not None,
    }
    if event_type in valid:
        if not valid[event_type]:
            raise ChangeEventDataError(
                f"У события {event_id} стороны не соответствуют типу изменения."
            )
        return
    if current is None or previous is None:
        raise ChangeEventDataError(
            f"У события {event_id} отсутствует обязательная сторона сравнения."
        )
    values = {
        ChangeEventType.TITLE_CHANGED: (current.title, previous.title),
        ChangeEventType.DESCRIPTION_CHANGED: (
            current.description,
            previous.description,
        ),
        ChangeEventType.H1_CHANGED: (current.h1, previous.h1),
        ChangeEventType.TEXT_CHANGED: (
            current.normalized_text,
            previous.normalized_text,
        ),
        ChangeEventType.INTERNAL_LINKS_CHANGED: (
            current.internal_links,
            previous.internal_links,
        ),
    }
    if values[event_type][0] == values[event_type][1]:
        raise ChangeEventDataError(
            f"Значения события {event_id} не соответствуют типу изменения."
        )


def _event_type(event: SnapshotChangeEvent) -> ChangeEventType:
    try:
        return ChangeEventType(event.event_type)
    except ValueError as error:
        raise ChangeEventDataError(
            f"У события {event.id} указан неизвестный тип."
        ) from error


def _importance(event: SnapshotChangeEvent) -> ChangeImportance:
    try:
        return ChangeImportance(event.importance)
    except ValueError as error:
        raise ChangeEventDataError(
            f"У события {event.id} указана неизвестная важность."
        ) from error


def _change_ratio(event: SnapshotChangeEvent) -> Fraction | None:
    numerator = event.change_ratio_numerator
    denominator = event.change_ratio_denominator
    if numerator is None and denominator is None:
        return None
    if numerator is None or denominator is None or denominator <= 0:
        raise ChangeEventDataError(
            f"У события {event.id} повреждена точная доля изменения."
        )
    return Fraction(numerator, denominator)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
