"""Транзакционное сохранение подтверждённого импорта Search Console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Callable

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .gsc_csv import ValidatedMetric
from .models import GSCImport, GSCPageMetric, Site


@dataclass(frozen=True)
class ImportOutcome:
    import_id: int
    row_count: int
    added_count: int
    updated_count: int
    unchanged_count: int


def save_import(
    engine: Engine,
    *,
    site_id: int,
    filename: str,
    period_start: date,
    period_end: date,
    delimiter: str,
    metrics: tuple[ValidatedMetric, ...],
    now_provider: Callable[[], datetime] | None = None,
) -> ImportOutcome:
    """В одной переносимой ORM-транзакции создать журнал и обновить показатели."""

    now = (now_provider or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None:
        raise ValueError("Время импорта должно содержать часовой пояс.")
    now = now.astimezone(UTC)
    with Session(engine) as session:
        if session.get(Site, site_id) is None:
            raise ValueError("Выбранный сайт больше не существует.")
        import_record = GSCImport(
            site_id=site_id,
            filename=filename,
            period_start=period_start,
            period_end=period_end,
            imported_at=now,
            row_count=len(metrics),
            added_count=0,
            updated_count=0,
            unchanged_count=0,
            delimiter=delimiter,
        )
        session.add(import_record)
        session.flush()
        assert import_record.id is not None

        existing_rows = session.exec(
            select(GSCPageMetric).where(
                GSCPageMetric.site_id == site_id,
                GSCPageMetric.period_start == period_start,
                GSCPageMetric.period_end == period_end,
            )
        ).all()
        existing = {row.normalized_url: row for row in existing_rows}
        added = updated = unchanged = 0
        for metric in metrics:
            row = existing.get(metric.normalized_url)
            if row is None:
                row = GSCPageMetric(
                    site_id=site_id,
                    period_start=period_start,
                    period_end=period_end,
                    normalized_url=metric.normalized_url,
                    clicks=metric.clicks,
                    impressions=metric.impressions,
                    average_position_text=metric.average_position_text,
                    last_import_id=import_record.id,
                    updated_at=now,
                )
                added += 1
            elif (
                row.clicks == metric.clicks
                and row.impressions == metric.impressions
                and row.average_position_text == metric.average_position_text
            ):
                unchanged += 1
                row.last_import_id = import_record.id
                row.updated_at = now
            else:
                updated += 1
                row.clicks = metric.clicks
                row.impressions = metric.impressions
                row.average_position_text = metric.average_position_text
                row.last_import_id = import_record.id
                row.updated_at = now
            session.add(row)

        import_record.added_count = added
        import_record.updated_count = updated
        import_record.unchanged_count = unchanged
        session.add(import_record)
        session.commit()
        return ImportOutcome(import_record.id, len(metrics), added, updated, unchanged)
