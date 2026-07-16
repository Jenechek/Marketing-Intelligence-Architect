"""Read-only адаптер загрузки пары завершённых снимков из хранилища."""

from sqlalchemy import and_, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import CrawlPageRecord, CrawlPageSnapshot, CrawlRun
from .snapshot_page_matching import (
    CompletedSnapshotPair,
    SnapshotPageReference,
    match_snapshot_pages,
)


COMPLETED_STATUS = "completed"


class CrawlRunNotFoundError(LookupError):
    """Запрошенный запуск обхода отсутствует."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__(f"Запуск обхода с идентификатором {run_id} не найден.")


class CrawlRunNotCompletedError(ValueError):
    """Запрошенный запуск нельзя использовать как завершённый снимок."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__(
            f"Запуск обхода с идентификатором {run_id} не завершён полностью "
            "или не имеет времени завершения."
        )


def load_completed_snapshot_pair(
    engine: Engine,
    current_run_id: int,
) -> CompletedSnapshotPair:
    """Загрузить и сопоставить выбранный completed-запуск и его предшественника."""

    with Session(engine) as session:
        current_run = session.get(CrawlRun, current_run_id)
        if current_run is None:
            raise CrawlRunNotFoundError(current_run_id)
        if (
            current_run.status != COMPLETED_STATUS
            or current_run.completed_at is None
        ):
            raise CrawlRunNotCompletedError(current_run_id)

        previous_run = session.exec(
            select(CrawlRun)
            .where(
                CrawlRun.site_id == current_run.site_id,
                CrawlRun.status == COMPLETED_STATUS,
                CrawlRun.completed_at.is_not(None),
                or_(
                    CrawlRun.completed_at < current_run.completed_at,
                    and_(
                        CrawlRun.completed_at == current_run.completed_at,
                        CrawlRun.id < current_run_id,
                    ),
                ),
            )
            .order_by(CrawlRun.completed_at.desc(), CrawlRun.id.desc())
        ).first()

        current_pages = _load_snapshot_pages(session, current_run_id)
        previous_run_id = previous_run.id if previous_run is not None else None
        previous_pages = (
            _load_snapshot_pages(session, previous_run_id)
            if previous_run_id is not None
            else None
        )

    return CompletedSnapshotPair(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        match_result=match_snapshot_pages(current_pages, previous_pages),
    )


def _load_snapshot_pages(
    session: Session,
    run_id: int,
) -> tuple[SnapshotPageReference, ...]:
    rows = session.exec(
        select(CrawlPageRecord.id, CrawlPageRecord.url)
        .join(
            CrawlPageSnapshot,
            CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
        )
        .where(CrawlPageRecord.crawl_run_id == run_id)
        .order_by(CrawlPageRecord.id)
    ).all()
    return tuple(
        SnapshotPageReference(identifier=record_id, url=url)
        for record_id, url in rows
    )
