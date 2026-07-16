"""Детерминированное извлечение содержимого HTML-страницы."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


_EXCLUDED_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "dialog",
    "button",
    "input",
    "select",
    "textarea",
    "iframe",
    "canvas",
    "svg",
}
_EXCLUDED_ROLES = {
    "navigation",
    "banner",
    "contentinfo",
    "complementary",
    "form",
    "dialog",
}
_WHITESPACE = re.compile(r"\s+")
_SIMPLE_AMOUNT = re.compile(r"\d+(?:[.,]\d+)?")
_CURRENCY = re.compile(r"[A-Za-z]{3}")
_PRICE_KINDS = {
    "price": "price",
    "lowPrice": "low",
    "highPrice": "high",
}


@dataclass(frozen=True)
class PagePrice:
    """Надёжно обнаруженная цена из структурированной разметки."""

    amount: Decimal
    currency: str | None
    kind: str
    source: str


@dataclass(frozen=True)
class PageData:
    """Извлечённые данные успешно разобранной HTML-страницы."""

    checked_at: datetime
    title: str | None
    description: str | None
    h1: str | None
    normalized_text: str
    content_hash: str
    internal_links: tuple[str, ...]
    prices: tuple[PagePrice, ...] = ()


def extract_page_data(html: str, internal_links: tuple[str, ...]) -> PageData:
    """Извлечь данные страницы без сети, хранения и поиска ссылок."""

    soup = BeautifulSoup(html, "lxml")
    prices = _extract_prices(soup)
    title = _first_element_text(soup.find("title"))
    description = _description(soup)
    h1 = _first_element_text(soup.find("h1"))

    root = soup.body if soup.body is not None else soup
    _remove_excluded_content(root)
    _replace_images_with_alt(root)
    normalized_text = normalize_main_text(root.get_text(separator=" "))
    content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

    return PageData(
        checked_at=datetime.now(timezone.utc),
        title=title,
        description=description,
        h1=h1,
        normalized_text=normalized_text,
        content_hash=content_hash,
        internal_links=internal_links,
        prices=prices,
    )


def _extract_prices(soup: BeautifulSoup) -> tuple[PagePrice, ...]:
    prices: list[PagePrice] = []
    for script in soup.find_all("script"):
        script_type = script.get("type")
        if (
            not isinstance(script_type, str)
            or script_type.casefold() != "application/ld+json"
        ):
            continue
        try:
            payload = json.loads(
                script.string or script.get_text(),
                parse_float=Decimal,
                parse_constant=lambda _: None,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        _walk_json_ld(payload, prices)

    _extract_microdata(soup, prices)

    unique: list[PagePrice] = []
    seen: set[tuple[str, Decimal, str | None]] = set()
    for price in prices:
        key = (price.kind, price.amount, price.currency)
        if key not in seen:
            seen.add(key)
            unique.append(price)
    return tuple(unique)


def _walk_json_ld(
    value: object,
    prices: list[PagePrice],
    *,
    inherited_currency: str | None = None,
    forced_type: str | None = None,
    suppress_unit_price: bool = False,
) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_json_ld(
                item,
                prices,
                inherited_currency=inherited_currency,
                forced_type=forced_type,
                suppress_unit_price=suppress_unit_price,
            )
        return
    if not isinstance(value, dict):
        return

    entity_types = _schema_types(value.get("@type"))
    if forced_type is not None:
        entity_types.add(forced_type)
    own_currency = _normalize_currency(value.get("priceCurrency"))
    currency = own_currency or inherited_currency
    direct_offer_price = None

    if "Offer" in entity_types:
        direct_offer_price = _normalize_amount(value.get("price"))
        if direct_offer_price is not None:
            prices.append(PagePrice(direct_offer_price, currency, "price", "json-ld"))
    if "AggregateOffer" in entity_types:
        _append_mapping_price(prices, value, "lowPrice", currency, "json-ld")
        _append_mapping_price(prices, value, "highPrice", currency, "json-ld")
    if (
        "UnitPriceSpecification" in entity_types
        and not suppress_unit_price
        and not _has_price_type(value)
    ):
        _append_mapping_price(prices, value, "price", currency, "json-ld")

    for key, child in value.items():
        if key == "@type":
            continue
        child_forced_type = (
            "UnitPriceSpecification" if key == "priceSpecification" else None
        )
        _walk_json_ld(
            child,
            prices,
            inherited_currency=currency,
            forced_type=child_forced_type,
            suppress_unit_price=(
                key == "priceSpecification" and direct_offer_price is not None
            ),
        )


def _extract_microdata(soup: BeautifulSoup, prices: list[PagePrice]) -> None:
    for scope in soup.find_all(attrs={"itemscope": True}):
        if not isinstance(scope, Tag):
            continue
        entity_types = _schema_types(scope.get("itemtype"))
        currency = _normalize_currency(_microdata_value(scope, "priceCurrency"))

        if "Offer" in entity_types:
            direct_amount = _normalize_amount(_microdata_value(scope, "price"))
            if direct_amount is not None:
                prices.append(PagePrice(direct_amount, currency, "price", "microdata"))
            else:
                for specification in _microdata_property_scopes(
                    scope, "priceSpecification"
                ):
                    if not _microdata_has_price_type(specification):
                        amount = _normalize_amount(
                            _microdata_value(specification, "price")
                        )
                        specification_currency = _normalize_currency(
                            _microdata_value(specification, "priceCurrency")
                        )
                        if amount is not None:
                            prices.append(
                                PagePrice(
                                    amount,
                                    specification_currency or currency,
                                    "price",
                                    "microdata",
                                )
                            )
        if "AggregateOffer" in entity_types:
            _append_microdata_price(prices, scope, "lowPrice", currency)
            _append_microdata_price(prices, scope, "highPrice", currency)
        if "UnitPriceSpecification" in entity_types:
            parent_offer = _parent_microdata_offer(scope)
            parent_has_price = (
                parent_offer is not None
                and _normalize_amount(_microdata_value(parent_offer, "price"))
                is not None
            )
            if not parent_has_price and not _microdata_has_price_type(scope):
                inherited = (
                    _normalize_currency(
                        _microdata_value(parent_offer, "priceCurrency")
                    )
                    if parent_offer is not None
                    else None
                )
                _append_microdata_price(
                    prices, scope, "price", currency or inherited
                )


def _append_mapping_price(
    prices: list[PagePrice],
    mapping: dict[object, object],
    property_name: str,
    currency: str | None,
    source: str,
) -> None:
    amount = _normalize_amount(mapping.get(property_name))
    if amount is not None:
        prices.append(
            PagePrice(amount, currency, _PRICE_KINDS[property_name], source)
        )


def _append_microdata_price(
    prices: list[PagePrice],
    scope: Tag,
    property_name: str,
    currency: str | None,
) -> None:
    amount = _normalize_amount(_microdata_value(scope, property_name))
    if amount is not None:
        prices.append(
            PagePrice(
                amount,
                currency,
                _PRICE_KINDS[property_name],
                "microdata",
            )
        )


def _schema_types(value: object) -> set[str]:
    values = value if isinstance(value, list) else [value]
    result: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        for item_type in item.split():
            name = item_type.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            if name in {"Offer", "AggregateOffer", "UnitPriceSpecification"}:
                result.add(name)
    return result


def _normalize_amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, Decimal)):
        text = str(value).strip()
    else:
        return None
    if _SIMPLE_AMOUNT.fullmatch(text) is None:
        return None
    try:
        amount = Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _normalize_currency(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value.upper() if _CURRENCY.fullmatch(value) is not None else None


def _has_price_type(value: dict[object, object]) -> bool:
    price_type = value.get("priceType")
    return price_type is not None and str(price_type).strip() != ""


def _microdata_value(scope: Tag, property_name: str) -> str | None:
    for element in scope.find_all(attrs={"itemprop": True}):
        if not isinstance(element, Tag):
            continue
        itemprop = element.get("itemprop")
        if not isinstance(itemprop, str) or property_name not in itemprop.split():
            continue
        if _nearest_microdata_scope(element) is not scope:
            continue
        content = element.get("content")
        if isinstance(content, str):
            return content
        return element.get_text(separator=" ")
    return None


def _microdata_property_scopes(scope: Tag, property_name: str) -> list[Tag]:
    result: list[Tag] = []
    for element in scope.find_all(attrs={"itemscope": True, "itemprop": True}):
        if not isinstance(element, Tag):
            continue
        itemprop = element.get("itemprop")
        if (
            isinstance(itemprop, str)
            and property_name in itemprop.split()
            and _parent_microdata_scope(element) is scope
        ):
            result.append(element)
    return result


def _nearest_microdata_scope(element: Tag) -> Tag | None:
    if element.has_attr("itemscope"):
        return element
    return _parent_microdata_scope(element)


def _parent_microdata_scope(element: Tag) -> Tag | None:
    for parent in element.parents:
        if isinstance(parent, Tag) and parent.has_attr("itemscope"):
            return parent
    return None


def _parent_microdata_offer(scope: Tag) -> Tag | None:
    parent = _parent_microdata_scope(scope)
    while parent is not None:
        if "Offer" in _schema_types(parent.get("itemtype")):
            return parent
        parent = _parent_microdata_scope(parent)
    return None


def _microdata_has_price_type(scope: Tag) -> bool:
    value = _microdata_value(scope, "priceType")
    return value is not None and value.strip() != ""


def normalize_readable_text(value: str) -> str:
    """Нормализовать читаемые метаданные без изменения регистра и пунктуации."""

    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()


def normalize_main_text(value: str) -> str:
    """Нормализовать основной текст в утверждённом порядке."""

    value = unicodedata.normalize("NFC", value)
    value = value.casefold()
    value = "".join(
        " " if unicodedata.category(character)[0] in {"P", "S"} else character
        for character in value
    )
    return _WHITESPACE.sub(" ", value).strip()


def _first_element_text(element: Tag | None) -> str | None:
    if element is None:
        return None
    return normalize_readable_text(element.get_text(separator=" "))


def _description(soup: BeautifulSoup) -> str | None:
    for meta in soup.find_all("meta"):
        name = meta.get("name")
        if isinstance(name, str) and name.casefold() == "description":
            content = meta.get("content")
            return normalize_readable_text(content if isinstance(content, str) else "")
    return None


def _remove_excluded_content(root: Tag | BeautifulSoup) -> None:
    for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    for element in list(root.find_all(True)):
        if element.parent is None:
            continue
        role = element.get("role")
        roles = role.split() if isinstance(role, str) else []
        aria_hidden = element.get("aria-hidden")
        if (
            element.name.casefold() in _EXCLUDED_TAGS
            or element.has_attr("hidden")
            or (
                isinstance(aria_hidden, str)
                and aria_hidden.strip().casefold() == "true"
            )
            or any(item.casefold() in _EXCLUDED_ROLES for item in roles)
        ):
            element.decompose()


def _replace_images_with_alt(root: Tag | BeautifulSoup) -> None:
    for image in list(root.find_all("img")):
        if image.parent is None:
            continue
        alt = image.get("alt")
        if isinstance(alt, str):
            image.replace_with(NavigableString(alt))
        else:
            image.decompose()
