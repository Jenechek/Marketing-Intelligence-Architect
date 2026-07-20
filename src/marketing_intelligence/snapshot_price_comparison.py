"""Чистое консервативное сравнение сохранённых цен по DEC-028."""

from dataclasses import dataclass
from decimal import Decimal

from .snapshot_comparison_input import MatchedSnapshotPageVersions, SnapshotPriceValue


@dataclass(frozen=True, slots=True)
class PriceProfile:
    kind: str
    currency: str
    low: Decimal
    high: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PriceChange:
    url: str
    previous_page_record_id: int
    current_page_record_id: int
    previous: PriceProfile
    current: PriceProfile


def compare_page_price(page: MatchedSnapshotPageVersions) -> PriceChange | None:
    """Вернуть одно изменение только для двух однозначно сравнимых профилей."""

    previous = build_price_profile(page.previous.prices)
    current = build_price_profile(page.current.prices)
    if (
        previous is None
        or current is None
        or previous.kind != current.kind
        or previous.currency != current.currency
        or (previous.low, previous.high) == (current.low, current.high)
    ):
        return None
    return PriceChange(
        url=page.url,
        previous_page_record_id=page.previous.identifier,
        current_page_record_id=page.current.identifier,
        previous=previous,
        current=current,
    )


def build_price_profile(prices: tuple[SnapshotPriceValue, ...]) -> PriceProfile | None:
    """Распознать строго один обычный price либо строгую пару low/high."""

    if any(
        price.amount is None
        or not price.amount.is_finite()
        or price.amount < 0
        or not price.currency
        for price in prices
    ):
        return None
    if len(prices) == 1 and prices[0].kind == "price":
        price = prices[0]
        assert price.amount is not None and price.currency is not None
        return PriceProfile("price", price.currency, price.amount)
    if len(prices) == 2 and {price.kind for price in prices} == {"low", "high"}:
        low = next(price for price in prices if price.kind == "low")
        high = next(price for price in prices if price.kind == "high")
        if low.currency != high.currency or low.amount > high.amount:
            return None
        assert low.amount is not None and high.amount is not None
        assert low.currency is not None
        return PriceProfile("range", low.currency, low.amount, high.amount)
    return None
