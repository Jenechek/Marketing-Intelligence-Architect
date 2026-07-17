"""Переносимое идемпотентное сохранение событий сравнения снимков."""

from fractions import Fraction

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .change_event import ChangeEventType
from .models import SnapshotChangeEvent
from .snapshot_comparison_aggregation import (
    ChangedSnapshotPageComparison,
    CompletedSnapshotComparisonResult,
)
from .snapshot_metadata_comparison import MetadataField


_METADATA_EVENT_TYPES = {
    MetadataField.TITLE: ChangeEventType.TITLE_CHANGED,
    MetadataField.DESCRIPTION: ChangeEventType.DESCRIPTION_CHANGED,
    MetadataField.H1: ChangeEventType.H1_CHANGED,
}


def save_snapshot_comparison_events(
    engine: Engine,
    comparison: CompletedSnapshotComparisonResult,
) -> int:
    """Сохранить отсутствующие события пары одной транзакцией."""

    events = _build_events(comparison)
    if not events:
        return 0

    with Session(engine) as session:
        existing_keys = set(
            session.exec(
                select(
                    SnapshotChangeEvent.event_type,
                    SnapshotChangeEvent.url,
                ).where(
                    SnapshotChangeEvent.current_run_id
                    == comparison.current_run_id,
                    SnapshotChangeEvent.previous_run_id
                    == comparison.previous_run_id,
                )
            ).all()
        )
        new_events = [
            event
            for event in events
            if (event.event_type, event.url) not in existing_keys
        ]
        session.add_all(new_events)
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
        return len(new_events)


def _build_events(
    comparison: CompletedSnapshotComparisonResult,
) -> tuple[SnapshotChangeEvent, ...]:
    if comparison.creates_baseline:
        return ()
    if comparison.previous_run_id is None:
        raise ValueError("Для событий сравнения отсутствует предыдущий запуск.")

    common = {
        "current_run_id": comparison.current_run_id,
        "previous_run_id": comparison.previous_run_id,
        "current_completed_at": comparison.current_completed_at,
    }
    events: list[SnapshotChangeEvent] = []
    for page in comparison.new_pages:
        events.append(
            SnapshotChangeEvent(
                **common,
                event_type=ChangeEventType.PAGE_ADDED.value,
                url=page.url,
                current_page_record_id=page.current.identifier,
                previous_page_record_id=None,
                importance=page.importance.value,
                weight=page.weight,
            )
        )
    for page in comparison.removed_pages:
        events.append(
            SnapshotChangeEvent(
                **common,
                event_type=ChangeEventType.PAGE_REMOVED.value,
                url=page.url,
                current_page_record_id=None,
                previous_page_record_id=page.previous.identifier,
                importance=page.importance.value,
                weight=page.weight,
            )
        )
    for page in comparison.changed_pages:
        events.extend(_changed_page_events(page, common))
    return tuple(events)


def _changed_page_events(
    page: ChangedSnapshotPageComparison,
    common: dict[str, object],
) -> tuple[SnapshotChangeEvent, ...]:
    identity = {
        **common,
        "url": page.url,
        "current_page_record_id": page.current.identifier,
        "previous_page_record_id": page.previous.identifier,
    }
    events = [
        SnapshotChangeEvent(
            **identity,
            event_type=_METADATA_EVENT_TYPES[change.field].value,
            importance=change.importance.value,
            weight=change.weight,
        )
        for change in page.metadata_changes
    ]
    if page.text_change is not None:
        events.append(
            SnapshotChangeEvent(
                **identity,
                event_type=ChangeEventType.TEXT_CHANGED.value,
                importance=page.text_change.importance.value,
                weight=page.text_change.weight,
                text_distance=page.text_change.distance,
                **_ratio_fields(page.text_change.change_ratio),
            )
        )
    if page.internal_links_change is not None:
        events.append(
            SnapshotChangeEvent(
                **identity,
                event_type=ChangeEventType.INTERNAL_LINKS_CHANGED.value,
                importance=page.internal_links_change.importance.value,
                weight=page.internal_links_change.weight,
                **_ratio_fields(page.internal_links_change.change_ratio),
            )
        )
    return tuple(events)


def _ratio_fields(ratio: Fraction) -> dict[str, int]:
    return {
        "change_ratio_numerator": ratio.numerator,
        "change_ratio_denominator": ratio.denominator,
    }
