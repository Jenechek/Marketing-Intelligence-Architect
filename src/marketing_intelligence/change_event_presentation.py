"""Русское server-rendered представление событий изменения."""

from dataclasses import dataclass
from enum import Enum

from .change_event import ChangeEventType, HistoryEventType, PriceChangeEventType
from .change_event_detail import ChangeEventDetail, PriceValues, SnapshotValues
from .change_importance import ChangeImportance


EVENT_TYPE_TITLES = {
    ChangeEventType.PAGE_ADDED: "Новая страница",
    ChangeEventType.PAGE_REMOVED: "Удалённая страница",
    ChangeEventType.TITLE_CHANGED: "Изменение Title",
    ChangeEventType.DESCRIPTION_CHANGED: "Изменение Description",
    ChangeEventType.H1_CHANGED: "Изменение H1",
    ChangeEventType.TEXT_CHANGED: "Изменение текста",
    ChangeEventType.INTERNAL_LINKS_CHANGED: "Изменение внутренних ссылок",
    PriceChangeEventType.PRICE_CHANGED: "Изменение цены",
}

IMPORTANCE_TITLES = {
    ChangeImportance.LOW: "Низкая",
    ChangeImportance.MEDIUM: "Средняя",
    ChangeImportance.HIGH: "Высокая",
}

EVENT_EXPLANATIONS = {
    ChangeEventType.PAGE_ADDED: (
        "В текущем завершённом обходе найдена страница, которой не было "
        "в предыдущем завершённом обходе."
    ),
    ChangeEventType.PAGE_REMOVED: (
        "Страница была в предыдущем завершённом обходе, но отсутствует "
        "в текущем завершённом обходе."
    ),
    ChangeEventType.TITLE_CHANGED: (
        "Значение Title страницы отличается от предыдущего завершённого обхода."
    ),
    ChangeEventType.DESCRIPTION_CHANGED: (
        "Значение Description страницы отличается от предыдущего "
        "завершённого обхода."
    ),
    ChangeEventType.H1_CHANGED: (
        "Значение H1 страницы отличается от предыдущего завершённого обхода."
    ),
    ChangeEventType.TEXT_CHANGED: (
        "Нормализованный текст страницы отличается от предыдущего "
        "завершённого обхода."
    ),
    ChangeEventType.INTERNAL_LINKS_CHANGED: (
        "Набор внутренних ссылок страницы отличается от предыдущего "
        "завершённого обхода."
    ),
    PriceChangeEventType.PRICE_CHANGED: (
        "Точная цена однозначной товарной посадочной страницы отличается "
        "от предыдущего завершённого обхода."
    ),
}


class ValueState(str, Enum):
    ABSENT_SIDE = "absent_side"
    NONE = "none"
    EMPTY_TEXT = "empty_text"
    EMPTY_LINKS = "empty_links"
    TEXT = "text"
    LINKS = "links"
    PRICE = "price"


@dataclass(frozen=True, slots=True)
class PresentedSide:
    state: ValueState
    text: str | None = None
    links: tuple[str, ...] = ()
    currency: str | None = None
    low: str | None = None
    high: str | None = None


def event_type_title(event_type: HistoryEventType) -> str:
    return EVENT_TYPE_TITLES[event_type]


def importance_title(importance: ChangeImportance | None) -> str:
    if importance is None:
        return "Не оценивалась"
    return IMPORTANCE_TITLES[importance]


def event_explanation(event_type: HistoryEventType) -> str:
    """Вернуть детерминированное фактическое объяснение типа события."""

    return EVENT_EXPLANATIONS[event_type]


def present_sides(
    detail: ChangeEventDetail,
) -> tuple[PresentedSide, PresentedSide]:
    """Вернуть Стало первым и Было вторым с явными состояниями значений."""

    return (
        _present_side(detail.event_type, detail.url, detail.current),
        _present_side(detail.event_type, detail.url, detail.previous),
    )


def _present_side(
    event_type: HistoryEventType,
    url: str,
    snapshot: SnapshotValues | PriceValues | None,
) -> PresentedSide:
    if snapshot is None:
        return PresentedSide(ValueState.ABSENT_SIDE)
    if event_type is PriceChangeEventType.PRICE_CHANGED:
        assert isinstance(snapshot, PriceValues)
        return PresentedSide(
            ValueState.PRICE,
            currency=snapshot.currency,
            low=snapshot.low,
            high=snapshot.high,
        )
    assert isinstance(snapshot, SnapshotValues)
    if event_type in {ChangeEventType.PAGE_ADDED, ChangeEventType.PAGE_REMOVED}:
        return PresentedSide(ValueState.TEXT, text=url)
    if event_type is ChangeEventType.INTERNAL_LINKS_CHANGED:
        if not snapshot.internal_links:
            return PresentedSide(ValueState.EMPTY_LINKS)
        return PresentedSide(ValueState.LINKS, links=snapshot.internal_links)
    value = {
        ChangeEventType.TITLE_CHANGED: snapshot.title,
        ChangeEventType.DESCRIPTION_CHANGED: snapshot.description,
        ChangeEventType.H1_CHANGED: snapshot.h1,
        ChangeEventType.TEXT_CHANGED: snapshot.normalized_text,
    }[event_type]
    if value is None:
        return PresentedSide(ValueState.NONE)
    if value == "":
        return PresentedSide(ValueState.EMPTY_TEXT)
    return PresentedSide(ValueState.TEXT, text=value)
