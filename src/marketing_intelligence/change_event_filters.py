"""Валидация пользовательского состояния списка событий изменений."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from urllib.parse import urlencode

from .change_event import ChangeEventType, HistoryEventType, PriceChangeEventType


EVENTS_PER_PAGE = 20


@dataclass(frozen=True, slots=True)
class ChangeEventListState:
    """Проверенные фильтры и номер страницы без пользовательского URL возврата."""

    site_id_value: str
    site_id: int | None
    event_type_value: str
    date_from_value: str
    date_to_value: str
    page_value: str
    event_type: HistoryEventType | None
    from_time: datetime | None
    before_time: datetime | None
    page: int

    @property
    def has_filters(self) -> bool:
        return bool(
            self.event_type_value
            or self.date_from_value
            or self.date_to_value
        )

    def query(self, *, page: int | None = None) -> str:
        """Собрать строку только из разрешённых и уже проверенных параметров."""

        values: list[tuple[str, str]] = []
        if self.site_id_value:
            values.append(("site_id", self.site_id_value))
        if self.event_type_value:
            values.append(("event_type", self.event_type_value))
        if self.date_from_value:
            values.append(("date_from", self.date_from_value))
        if self.date_to_value:
            values.append(("date_to", self.date_to_value))
        target_page = self.page if page is None else page
        if target_page != 1:
            values.append(("page", str(target_page)))
        return urlencode(values)


@dataclass(frozen=True, slots=True)
class ChangeEventListForm:
    site_id: str
    event_type: str
    date_from: str
    date_to: str
    page: str


def parse_change_event_list_state(
    *,
    site_id: str = "",
    event_type: str,
    date_from: str,
    date_to: str,
    page: str,
    local_timezone: tzinfo | None = None,
) -> tuple[ChangeEventListState | None, dict[str, str]]:
    """Проверить GET-параметры и перевести границы локальных дат в UTC."""

    errors: dict[str, str] = {}
    parsed_site_id: int | None = None
    if site_id:
        try:
            parsed_site_id = int(site_id)
            if str(parsed_site_id) != site_id or parsed_site_id < 1:
                raise ValueError
        except ValueError:
            errors["site_id"] = "Выберите существующий сайт из списка."
    parsed_type: HistoryEventType | None = None
    if event_type:
        try:
            parsed_type = (
                PriceChangeEventType.PRICE_CHANGED
                if event_type == PriceChangeEventType.PRICE_CHANGED.value
                else ChangeEventType(event_type)
            )
        except ValueError:
            errors["event_type"] = "Выберите один из доступных типов события."

    parsed_from = _parse_date(date_from, "date_from", "Дата «с» указана неверно.", errors)
    parsed_to = _parse_date(date_to, "date_to", "Дата «по» указана неверно.", errors)
    if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
        errors["date_range"] = "Дата «с» не может быть позже даты «по»."

    try:
        parsed_page = int(page)
        if str(parsed_page) != page or parsed_page < 1:
            raise ValueError
    except ValueError:
        parsed_page = 1
        errors["page"] = "Номер страницы должен быть положительным целым числом."

    if errors:
        return None, errors

    try:
        from_time = _local_midnight_to_utc(parsed_from, local_timezone)
    except (OverflowError, ValueError):
        return None, {"date_from": "Дата выходит за поддерживаемый диапазон."}
    try:
        before_time = _local_midnight_to_utc(
            parsed_to + timedelta(days=1) if parsed_to is not None else None,
            local_timezone,
        )
    except (OverflowError, ValueError):
        return None, {"date_to": "Дата выходит за поддерживаемый диапазон."}
    return (
        ChangeEventListState(
            site_id_value=site_id,
            site_id=parsed_site_id,
            event_type_value=event_type,
            date_from_value=date_from,
            date_to_value=date_to,
            page_value=page,
            event_type=parsed_type,
            from_time=from_time,
            before_time=before_time,
            page=parsed_page,
        ),
        {},
    )


def change_event_list_url(
    site_id: int,
    state: ChangeEventListState,
    *,
    page: int | None = None,
) -> str:
    query = state.query(page=page)
    base = f"/sites/{site_id}/changes"
    return f"{base}?{query}" if query else base


def global_change_event_list_url(
    state: ChangeEventListState,
    *,
    page: int | None = None,
) -> str:
    """Собрать безопасную ссылку общей ленты из проверенного состояния."""

    query = state.query(page=page)
    return f"/changes?{query}" if query else "/changes"


def _parse_date(
    value: str,
    field: str,
    message: str,
    errors: dict[str, str],
) -> date | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        errors[field] = message
        return None


def _local_midnight_to_utc(
    value: date | None,
    local_timezone: tzinfo | None,
) -> datetime | None:
    if value is None:
        return None
    local_midnight = datetime.combine(value, time.min, tzinfo=local_timezone)
    return local_midnight.astimezone(UTC)
