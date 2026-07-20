"""Пакетная read-only подготовка безопасного экспорта истории."""

from __future__ import annotations

import codecs
import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from tempfile import SpooledTemporaryFile

from sqlalchemy import and_, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .change_event import ChangeEventType, HistoryEventType
from .change_event_detail import (
    ChangeEventDataError,
    ChangeEventDetail,
    PriceValues,
    SnapshotValues,
)
from .change_event_presentation import (
    ValueState,
    event_explanation,
    event_type_title,
    importance_title,
    present_sides,
)
from .change_event_query import ChangeEventItem, load_change_events
from .models import (
    CrawlPagePriceRecord,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
)
from .snapshot_comparison_input import SnapshotPriceValue
from .snapshot_price_comparison import build_price_profile


JSON_SCHEMA_VERSION = 1
EXPORT_BATCH_SIZE = 200
CSV_HEADERS = (
    "ID сайта",
    "Сайт",
    "URL сайта",
    "Источник",
    "ID события",
    "Тип события",
    "Обнаружено",
    "URL события",
    "Важность",
    "Вес важности",
    "Объяснение",
    "Статус просмотра",
    "Просмотрено в",
    "Стало",
    "Было",
)


@dataclass(slots=True)
class PreparedExport:
    file: SpooledTemporaryFile
    media_type: str
    filename: str

    def chunks(self, size: int = 64 * 1024):
        try:
            while chunk := self.file.read(size):
                yield chunk
        finally:
            self.file.close()


def prepare_change_event_export(
    engine: Engine,
    *,
    format_name: str,
    site_id: int | None,
    event_types: tuple[HistoryEventType, ...] | None,
    from_time: datetime | None,
    before_time: datetime | None,
    viewed: bool | None,
    local_timezone: tzinfo | None,
) -> PreparedExport:
    """Полностью проверить и записать файл до отправки клиенту."""

    if format_name not in {"json", "csv"}:
        raise ValueError("Неизвестный формат экспорта.")
    output = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    try:
        if format_name == "json":
            _write_json(
                output,
                engine,
                site_id=site_id,
                event_types=event_types,
                from_time=from_time,
                before_time=before_time,
                viewed=viewed,
            )
            media_type = "application/json; charset=utf-8"
        else:
            _write_csv(
                output,
                engine,
                site_id=site_id,
                event_types=event_types,
                from_time=from_time,
                before_time=before_time,
                viewed=viewed,
                local_timezone=local_timezone,
            )
            media_type = "text/csv; charset=utf-8"
        output.seek(0)
        scope = f"site-{site_id}" if site_id is not None else "all-sites"
        return PreparedExport(output, media_type, f"change-history-{scope}.{format_name}")
    except Exception:
        output.close()
        raise


def _iter_batches(engine: Engine, **filters):
    offset = 0
    while True:
        page = load_change_events(
            engine,
            **filters,
            limit=EXPORT_BATCH_SIZE,
            offset=offset,
        )
        if not page.items:
            return
        yield page.items, _load_details_batch(engine, page.items)
        offset += len(page.items)
        if offset >= page.total_count:
            return


def _write_json(output, engine: Engine, **filters) -> None:
    output.write(f'{{"schema_version":{JSON_SCHEMA_VERSION},"events":['.encode("ascii"))
    first = True
    for items, details in _iter_batches(engine, **filters):
        for item in items:
            if not first:
                output.write(b",")
            first = False
            payload = _json_event(item, details[(item.source, item.event_id)])
            output.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    output.write(b"]}\n")


def _write_csv(output, engine: Engine, *, local_timezone: tzinfo | None, **filters) -> None:
    output.write(codecs.BOM_UTF8)
    text = io.TextIOWrapper(output, encoding="utf-8", newline="", write_through=True)
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    for items, details in _iter_batches(engine, **filters):
        for item in items:
            detail = details[(item.source, item.event_id)]
            current, previous = present_sides(detail)
            writer.writerow(
                _safe_csv_cell(value)
                for value in (
                    item.site_id,
                    item.site_name,
                    item.site_url,
                    item.source,
                    item.event_id,
                    event_type_title(item.event_type),
                    item.current_completed_at.astimezone(local_timezone).isoformat(),
                    item.url,
                    importance_title(item.importance),
                    item.weight,
                    event_explanation(item.event_type),
                    "Просмотрено" if item.is_viewed else "Не просмотрено",
                    item.viewed_at.astimezone(local_timezone).isoformat()
                    if item.viewed_at
                    else "",
                    _human_side(current),
                    _human_side(previous),
                )
            )
    text.flush()
    text.detach()


def _json_event(item: ChangeEventItem, detail: ChangeEventDetail) -> dict:
    current, previous = present_sides(detail)
    return {
        "site_id": item.site_id,
        "site_name": item.site_name,
        "site_url": item.site_url,
        "source": item.source,
        "event_id": item.event_id,
        "event_type": item.event_type.value,
        "detected_at": _utc_iso(item.current_completed_at),
        "url": item.url,
        "importance": item.importance.value if item.importance else None,
        "importance_weight": item.weight,
        "explanation": event_explanation(item.event_type),
        "viewed": item.is_viewed,
        "viewed_at": _utc_iso(item.viewed_at) if item.viewed_at else None,
        "current": _json_side(current),
        "previous": _json_side(previous),
    }


def _json_side(side) -> dict:
    if side.state is ValueState.PRICE:
        profile = "price_range" if side.high is not None else "price"
        return {
            "state": profile,
            "currency": side.currency,
            "low": side.low,
            "high": side.high,
        }
    result = {"state": side.state.value}
    if side.state is ValueState.TEXT:
        result["value"] = side.text
    elif side.state is ValueState.LINKS:
        result["links"] = list(side.links)
    return result


def _human_side(side) -> str:
    if side.state is ValueState.ABSENT_SIDE:
        return "Сторона отсутствует"
    if side.state is ValueState.NONE:
        return "Нет значения"
    if side.state is ValueState.EMPTY_TEXT:
        return "Пустая строка"
    if side.state is ValueState.EMPTY_LINKS:
        return "Пустой список ссылок"
    if side.state is ValueState.LINKS:
        return "\n".join(side.links)
    if side.state is ValueState.PRICE:
        amount = side.low if side.high is None else f"{side.low}–{side.high}"
        return f"{amount} {side.currency}"
    return side.text or ""


def _safe_csv_cell(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _load_details_batch(
    engine: Engine,
    items: tuple[ChangeEventItem, ...],
) -> dict[tuple[str, int], ChangeEventDetail]:
    run_ids = {run_id for item in items for run_id in (item.current_run_id, item.previous_run_id)}
    page_ids = {
        page_id
        for item in items
        for page_id in (item.current_page_record_id, item.previous_page_record_id)
        if page_id is not None
    }
    with Session(engine) as session:
        runs = {
            run.id: run
            for run in session.exec(select(CrawlRun).where(CrawlRun.id.in_(run_ids))).all()
        }
        _validate_run_pairs(session, items, runs)
        pages = {
            page.id: (page, snapshot)
            for page, snapshot in session.exec(
                select(CrawlPageRecord, CrawlPageSnapshot)
                .join(
                    CrawlPageSnapshot,
                    CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
                )
                .where(CrawlPageRecord.id.in_(page_ids))
            ).all()
        }
        price_records: dict[int, list[CrawlPagePriceRecord]] = {}
        for record in session.exec(
            select(CrawlPagePriceRecord)
            .where(CrawlPagePriceRecord.crawl_page_snapshot_id.in_(page_ids))
            .order_by(
                CrawlPagePriceRecord.crawl_page_snapshot_id,
                CrawlPagePriceRecord.sequence_number,
            )
        ).all():
            price_records.setdefault(record.crawl_page_snapshot_id, []).append(record)

    result = {}
    for item in items:
        if item.source == "price":
            current = _price_side(item, item.current_page_record_id, pages, price_records, "Стало")
            previous = _price_side(item, item.previous_page_record_id, pages, price_records, "Было")
            if (
                current.profile != item.price_profile
                or previous.profile != item.price_profile
                or current.currency != item.price_currency
                or previous.currency != item.price_currency
                or (current.low, current.high) == (previous.low, previous.high)
            ):
                raise ChangeEventDataError(
                    f"Ценовые значения события {item.event_id} повреждены."
                )
        else:
            current = _snapshot_side(item, item.current_page_record_id, pages, "Стало")
            previous = _snapshot_side(item, item.previous_page_record_id, pages, "Было")
            _validate_snapshot_sides(item, current, previous)
        result[(item.source, item.event_id)] = ChangeEventDetail(
            event_id=item.event_id,
            event_type=item.event_type,
            url=item.url,
            current_completed_at=item.current_completed_at,
            importance=item.importance,
            weight=item.weight,
            current_run_id=item.current_run_id,
            previous_run_id=item.previous_run_id,
            current=current,
            previous=previous,
            text_distance=item.text_distance,
            change_ratio=item.change_ratio,
            viewed_at=item.viewed_at,
        )
    return result


def _validate_item_runs(item: ChangeEventItem, runs: dict[int, CrawlRun]) -> None:
    current = runs.get(item.current_run_id)
    previous = runs.get(item.previous_run_id)
    if (
        current is None
        or previous is None
        or current.site_id != item.site_id
        or previous.site_id != item.site_id
        or current.status != "completed"
        or previous.status != "completed"
        or current.completed_at is None
        or previous.completed_at is None
        or _database_utc(current.completed_at) != item.current_completed_at.astimezone(UTC)
    ):
        raise ChangeEventDataError(f"У события {item.event_id} повреждены ссылки на обходы.")


def _validate_run_pairs(session: Session, items, runs: dict[int, CrawlRun]) -> None:
    pairs = {}
    for item in items:
        _validate_item_runs(item, runs)
        current = runs[item.current_run_id]
        previous = runs[item.previous_run_id]
        current_key = (_database_utc(current.completed_at), current.id)
        previous_key = (_database_utc(previous.completed_at), previous.id)
        if previous_key >= current_key:
            raise ChangeEventDataError(
                f"У события {item.event_id} нарушен порядок завершённых обходов."
            )
        pairs[(item.site_id, current.id, previous.id)] = (current, previous)
    conditions = []
    for (site_id, current_id, previous_id), (current, previous) in pairs.items():
        conditions.append(
            and_(
                CrawlRun.site_id == site_id,
                CrawlRun.status == "completed",
                CrawlRun.completed_at.is_not(None),
                or_(
                    CrawlRun.completed_at < current.completed_at,
                    and_(
                        CrawlRun.completed_at == current.completed_at,
                        CrawlRun.id < current_id,
                    ),
                ),
                or_(
                    CrawlRun.completed_at > previous.completed_at,
                    and_(
                        CrawlRun.completed_at == previous.completed_at,
                        CrawlRun.id > previous_id,
                    ),
                ),
            )
        )
    if conditions and session.exec(
        select(CrawlRun.id).where(or_(*conditions)).limit(1)
    ).first() is not None:
        raise ChangeEventDataError("У экспортируемого события нарушена последовательность обходов.")


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _snapshot_side(item, page_id, pages, side_name) -> SnapshotValues | None:
    if page_id is None:
        return None
    pair = pages.get(page_id)
    expected_run = item.current_run_id if side_name == "Стало" else item.previous_run_id
    if pair is None or pair[0].crawl_run_id != expected_run or pair[0].url != item.url:
        raise ChangeEventDataError(f"Сторона «{side_name}» события повреждена.")
    snapshot = pair[1]
    try:
        links = json.loads(snapshot.internal_links_json)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ChangeEventDataError(f"Ссылки стороны «{side_name}» повреждены.") from error
    if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
        raise ChangeEventDataError(f"Ссылки стороны «{side_name}» повреждены.")
    return SnapshotValues(
        snapshot.title,
        snapshot.description,
        snapshot.h1,
        snapshot.normalized_text,
        tuple(links),
    )


def _price_side(item, page_id, pages, price_records, side_name) -> PriceValues:
    snapshot = _snapshot_side(item, page_id, pages, side_name)
    assert snapshot is not None and page_id is not None
    values = []
    for record in price_records.get(page_id, []):
        try:
            amount = record.amount
        except (ArithmeticError, ValueError) as error:
            raise ChangeEventDataError(f"Цена стороны «{side_name}» повреждена.") from error
        values.append(SnapshotPriceValue(amount, record.currency, record.kind, record.source))
    profile = build_price_profile(tuple(values))
    if profile is None:
        raise ChangeEventDataError(f"Цена стороны «{side_name}» неоднозначна.")
    return PriceValues(
        profile.kind,
        profile.currency,
        str(profile.low),
        str(profile.high) if profile.high is not None else None,
    )


def _validate_snapshot_sides(item, current, previous) -> None:
    expected = {
        ChangeEventType.PAGE_ADDED: current is not None and previous is None,
        ChangeEventType.PAGE_REMOVED: current is None and previous is not None,
    }
    if item.event_type in expected and not expected[item.event_type]:
        raise ChangeEventDataError(f"Стороны события {item.event_id} не соответствуют типу.")
    if item.event_type not in expected and (current is None or previous is None):
        raise ChangeEventDataError(f"У события {item.event_id} отсутствует сторона.")
    if current is None or previous is None:
        return
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
    if item.event_type in values and values[item.event_type][0] == values[item.event_type][1]:
        raise ChangeEventDataError(
            f"Значения события {item.event_id} не соответствуют типу."
        )
