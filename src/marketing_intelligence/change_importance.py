"""Общая точная классификация важности изменений содержимого страницы."""

from enum import Enum
from fractions import Fraction


class ChangeImportance(str, Enum):
    """Уровень важности изменения содержимого страницы."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def classify_change_ratio(
    change_ratio: Fraction,
) -> tuple[ChangeImportance, int]:
    """Классифицировать точную ненулевую долю по порогам DEC-024."""

    if change_ratio <= 0:
        raise ValueError("change_ratio must be positive")
    if change_ratio < Fraction(1, 10):
        return ChangeImportance.LOW, 1
    if change_ratio < Fraction(3, 10):
        return ChangeImportance.MEDIUM, 2
    return ChangeImportance.HIGH, 3
