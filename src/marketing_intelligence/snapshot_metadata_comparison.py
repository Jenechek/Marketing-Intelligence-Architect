"""Точное сравнение метаданных уже совпавшей пары страниц снимков."""

from dataclasses import dataclass
from enum import Enum


class MetadataField(str, Enum):
    """Поддерживаемое поле метаданных страницы."""

    TITLE = "title"
    DESCRIPTION = "description"
    H1 = "h1"


class ChangeImportance(str, Enum):
    """Уровень важности изменения содержимого страницы."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class MatchedPageMetadata:
    """Метаданные старой и новой страниц, уже сопоставленных по URL."""

    url: str
    previous_page_id: int | str
    current_page_id: int | str
    previous_title: str | None
    current_title: str | None
    previous_description: str | None
    current_description: str | None
    previous_h1: str | None
    current_h1: str | None


@dataclass(frozen=True)
class MetadataChange:
    """Одно неизменяемое изменение поля совпавшей пары страниц."""

    url: str
    previous_page_id: int | str
    current_page_id: int | str
    field: MetadataField
    previous_value: str | None
    current_value: str | None
    importance: ChangeImportance
    weight: int


_FIELD_RULES = (
    (
        MetadataField.TITLE,
        "previous_title",
        "current_title",
        ChangeImportance.HIGH,
        3,
    ),
    (
        MetadataField.DESCRIPTION,
        "previous_description",
        "current_description",
        ChangeImportance.MEDIUM,
        2,
    ),
    (
        MetadataField.H1,
        "previous_h1",
        "current_h1",
        ChangeImportance.HIGH,
        3,
    ),
)


def compare_matched_page_metadata(
    page: MatchedPageMetadata,
) -> tuple[MetadataChange, ...]:
    """Вернуть точные изменения Title, Description и H1 в стабильном порядке."""

    changes: list[MetadataChange] = []
    for field, previous_name, current_name, importance, weight in _FIELD_RULES:
        previous_value = getattr(page, previous_name)
        current_value = getattr(page, current_name)
        if previous_value == current_value:
            continue
        changes.append(
            MetadataChange(
                url=page.url,
                previous_page_id=page.previous_page_id,
                current_page_id=page.current_page_id,
                field=field,
                previous_value=previous_value,
                current_value=current_value,
                importance=importance,
                weight=weight,
            )
        )
    return tuple(changes)
