"""Переносимое кодирование точных денежных сумм для хранения."""

from decimal import Decimal, InvalidOperation
import re


_CANONICAL_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?")


def encode_decimal_text(value: Decimal) -> str:
    """Канонически представить неотрицательный конечный ``Decimal`` текстом."""

    if not value.is_finite() or (value.is_signed() and not value.is_zero()):
        raise ValueError("Сумма должна быть конечным неотрицательным Decimal.")
    if value.is_zero():
        return "0"

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def decode_decimal_text(value: str) -> Decimal:
    """Восстановить ``Decimal`` только из канонического сохранённого текста."""

    if _CANONICAL_DECIMAL.fullmatch(value) is None:
        raise ValueError("Сумма не является каноническим десятичным текстом.")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Не удалось восстановить точную сумму.") from error
    if encode_decimal_text(amount) != value:
        raise ValueError("Сумма не является каноническим десятичным текстом.")
    return amount
