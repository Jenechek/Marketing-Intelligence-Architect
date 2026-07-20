"""Идемпотентное переносимое хранение достоверных ценовых событий."""

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import PriceChangeEvent
from .snapshot_comparison_input import CompletedSnapshotComparisonInput
from .snapshot_price_comparison import compare_page_price


def save_price_change_events(
    engine: Engine, comparison: CompletedSnapshotComparisonInput
) -> int:
    if comparison.creates_baseline:
        return 0
    if comparison.previous_run_id is None:
        raise ValueError("Для ценовых событий отсутствует предыдущий запуск.")
    changes = tuple(
        change
        for page in comparison.matched_pages
        if (change := compare_page_price(page)) is not None
    )
    if not changes:
        return 0
    with Session(engine) as session:
        existing_urls = set(
            session.exec(
                select(PriceChangeEvent.url).where(
                    PriceChangeEvent.current_run_id == comparison.current_run_id,
                    PriceChangeEvent.previous_run_id == comparison.previous_run_id,
                )
            ).all()
        )
        events = [
            PriceChangeEvent(
                current_run_id=comparison.current_run_id,
                previous_run_id=comparison.previous_run_id,
                current_page_record_id=change.current_page_record_id,
                previous_page_record_id=change.previous_page_record_id,
                url=change.url,
                current_completed_at=comparison.current_completed_at,
                profile=change.current.kind,
                currency=change.current.currency,
            )
            for change in changes
            if change.url not in existing_urls
        ]
        session.add_all(events)
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
        return len(events)
