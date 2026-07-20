"""Транзакционные операции ручного состояния просмотра событий."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .models import (
    ChangeEventViewState,
    CrawlRun,
    PriceChangeEvent,
    SnapshotChangeEvent,
)


EVENT_SOURCES = {"snapshot", "price"}


@dataclass(frozen=True, slots=True)
class ViewStateResult:
    found: bool
    changed: bool
    viewed_at: datetime | None


def set_change_event_viewed(
    engine: Engine,
    *,
    site_id: int,
    source: str,
    event_id: int,
    viewed: bool,
    now: datetime | None = None,
) -> ViewStateResult:
    """Установить состояние после полной проверки принадлежности события сайту."""

    if source not in EVENT_SOURCES:
        raise ValueError("Неизвестный источник события.")
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
        raise ValueError("event_id должен быть положительным целым числом.")
    if isinstance(site_id, bool) or not isinstance(site_id, int) or site_id < 1:
        raise ValueError("site_id должен быть положительным целым числом.")
    if not isinstance(viewed, bool):
        raise ValueError("viewed должен быть логическим значением.")
    requested_time = now or datetime.now(UTC)
    if requested_time.tzinfo is None or requested_time.utcoffset() is None:
        raise ValueError("Время просмотра должно содержать часовой пояс.")
    viewed_at = requested_time.astimezone(UTC)

    event_model = SnapshotChangeEvent if source == "snapshot" else PriceChangeEvent
    state_field = (
        ChangeEventViewState.snapshot_change_event_id
        if source == "snapshot"
        else ChangeEventViewState.price_change_event_id
    )
    with Session(engine) as session:
        event_exists = session.exec(
            select(event_model.id)
            .join(CrawlRun, event_model.current_run_id == CrawlRun.id)
            .where(event_model.id == event_id, CrawlRun.site_id == site_id)
        ).first()
        if event_exists is None:
            return ViewStateResult(False, False, None)
        state = session.exec(
            select(ChangeEventViewState).where(state_field == event_id)
        ).first()
        if viewed:
            if state is not None:
                return ViewStateResult(True, False, state.viewed_at)
            state = ChangeEventViewState(
                snapshot_change_event_id=event_id if source == "snapshot" else None,
                price_change_event_id=event_id if source == "price" else None,
                viewed_at=viewed_at,
            )
            session.add(state)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.exec(
                    select(ChangeEventViewState).where(state_field == event_id)
                ).first()
                if existing is None:
                    raise
                return ViewStateResult(True, False, existing.viewed_at)
            return ViewStateResult(True, True, viewed_at)
        if state is None:
            return ViewStateResult(True, False, None)
        session.exec(delete(ChangeEventViewState).where(ChangeEventViewState.id == state.id))
        session.commit()
        return ViewStateResult(True, True, None)
