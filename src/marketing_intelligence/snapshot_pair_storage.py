"""Read-only адаптеры загрузки пары завершённых снимков из хранилища."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import CrawlPagePriceRecord, CrawlPageRecord, CrawlPageSnapshot, CrawlRun
from .snapshot_comparison_input import (
    CompletedSnapshotComparisonInput,
    MatchedSnapshotPageVersions,
    SnapshotPageVersion,
    SnapshotPriceValue,
)
from .snapshot_page_matching import (
    CompletedSnapshotPair,
    SnapshotPageReference,
    match_snapshot_pages,
)
from .price_persistence import decode_decimal_text


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


class InvalidInternalLinksJsonError(ValueError):
    """Ссылки страницы содержат повреждённый JSON."""

    def __init__(self, page_identifier: int) -> None:
        self.page_identifier = page_identifier
        super().__init__(
            "Внутренние ссылки страницы с идентификатором "
            f"{page_identifier} содержат повреждённый JSON."
        )


class InternalLinksNotArrayError(ValueError):
    """JSON внутренних ссылок страницы не является массивом."""

    def __init__(self, page_identifier: int) -> None:
        self.page_identifier = page_identifier
        super().__init__(
            "Внутренние ссылки страницы с идентификатором "
            f"{page_identifier} должны быть JSON-массивом."
        )


class InternalLinkNotStringError(ValueError):
    """JSON-массив внутренних ссылок содержит значение не строкового типа."""

    def __init__(self, page_identifier: int) -> None:
        self.page_identifier = page_identifier
        super().__init__(
            "Внутренние ссылки страницы с идентификатором "
            f"{page_identifier} должны содержать только строки."
        )


def load_completed_snapshot_pair(
    engine: Engine,
    current_run_id: int,
) -> CompletedSnapshotPair:
    """Загрузить и сопоставить выбранный completed-запуск и его предшественника."""

    with Session(engine) as session:
        _, previous_run = _select_completed_run_pair(session, current_run_id)

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


def load_completed_snapshot_comparison_input(
    engine: Engine,
    current_run_id: int,
) -> CompletedSnapshotComparisonInput:
    """Пакетно загрузить полное содержимое пары завершённых снимков."""

    with Session(engine) as session:
        current_run, previous_run = _select_completed_run_pair(
            session, current_run_id
        )
        previous_run_id = previous_run.id if previous_run is not None else None
        versions = _load_snapshot_page_versions(
            session,
            current_run_id=current_run_id,
            previous_run_id=previous_run_id,
        )

    current_versions = versions.get(current_run_id, ())
    previous_versions = (
        versions.get(previous_run_id, ()) if previous_run_id is not None else None
    )
    match_result = match_snapshot_pages(
        tuple(_page_reference(page) for page in current_versions),
        (
            tuple(_page_reference(page) for page in previous_versions)
            if previous_versions is not None
            else None
        ),
    )
    versions_by_identifier = {
        page.identifier: page
        for run_versions in versions.values()
        for page in run_versions
    }
    assert current_run.completed_at is not None
    return CompletedSnapshotComparisonInput(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        current_completed_at=_as_utc(current_run.completed_at),
        creates_baseline=match_result.creates_baseline,
        new_pages=match_result.current_only,
        removed_pages=match_result.previous_only,
        matched_pages=tuple(
            MatchedSnapshotPageVersions(
                previous=versions_by_identifier[match.previous.identifier],
                current=versions_by_identifier[match.current.identifier],
            )
            for match in match_result.matched
        ),
    )


def _select_completed_run_pair(
    session: Session,
    current_run_id: int,
) -> tuple[CrawlRun, CrawlRun | None]:
    current_run = session.get(CrawlRun, current_run_id)
    if current_run is None:
        raise CrawlRunNotFoundError(current_run_id)
    if current_run.status != COMPLETED_STATUS or current_run.completed_at is None:
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
    return current_run, previous_run


def _load_snapshot_page_versions(
    session: Session,
    *,
    current_run_id: int,
    previous_run_id: int | None,
) -> dict[int, tuple[SnapshotPageVersion, ...]]:
    run_ids = (
        (current_run_id, previous_run_id)
        if previous_run_id is not None
        else (current_run_id,)
    )
    rows = session.exec(
        select(
            CrawlPageRecord.crawl_run_id,
            CrawlPageRecord.id,
            CrawlPageRecord.url,
            CrawlPageSnapshot.checked_at,
            CrawlPageSnapshot.title,
            CrawlPageSnapshot.description,
            CrawlPageSnapshot.h1,
            CrawlPageSnapshot.normalized_text,
            CrawlPageSnapshot.content_hash,
            CrawlPageSnapshot.internal_links_json,
            CrawlPagePriceRecord.amount_text,
            CrawlPagePriceRecord.currency,
            CrawlPagePriceRecord.kind,
            CrawlPagePriceRecord.source,
        )
        .join(
            CrawlPageSnapshot,
            CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
        )
        .outerjoin(
            CrawlPagePriceRecord,
            CrawlPagePriceRecord.crawl_page_snapshot_id
            == CrawlPageSnapshot.crawl_page_record_id,
        )
        .where(CrawlPageRecord.crawl_run_id.in_(run_ids))
        .order_by(
            CrawlPageRecord.crawl_run_id,
            CrawlPageRecord.id,
            CrawlPagePriceRecord.sequence_number,
        )
    ).all()
    page_data: dict[int, tuple] = {}
    prices_by_page: dict[int, list[SnapshotPriceValue]] = {}
    for row in rows:
        page_data.setdefault(row.id, tuple(row[:10]))
        prices_by_page.setdefault(row.id, [])
        if row.amount_text is not None:
            try:
                amount = decode_decimal_text(row.amount_text)
            except (ArithmeticError, ValueError):
                amount = None
            prices_by_page[row.id].append(
                SnapshotPriceValue(
                    amount=amount,
                    currency=row.currency,
                    kind=row.kind,
                    source=row.source,
                )
            )
    by_run: dict[int, list[SnapshotPageVersion]] = {run_id: [] for run_id in run_ids}
    for row in page_data.values():
        (
            run_id,
            identifier,
            url,
            checked_at,
            title,
            description,
            h1,
            normalized_text,
            content_hash,
            internal_links_json,
        ) = row
        by_run[run_id].append(
            SnapshotPageVersion(
                identifier=identifier,
                url=url,
                checked_at=checked_at,
                title=title,
                description=description,
                h1=h1,
                normalized_text=normalized_text,
                content_hash=content_hash,
                internal_links=_decode_internal_links(
                    internal_links_json, identifier
                ),
                prices=tuple(prices_by_page[identifier]),
            )
        )
    return {run_id: tuple(run_versions) for run_id, run_versions in by_run.items()}


def _decode_internal_links(value: str, page_identifier: int) -> tuple[str, ...]:
    try:
        decoded: Any = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidInternalLinksJsonError(page_identifier) from error
    if not isinstance(decoded, list):
        raise InternalLinksNotArrayError(page_identifier)
    if not all(isinstance(link, str) for link in decoded):
        raise InternalLinkNotStringError(page_identifier)
    return tuple(decoded)


def _page_reference(page: SnapshotPageVersion) -> SnapshotPageReference:
    return SnapshotPageReference(identifier=page.identifier, url=page.url)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
