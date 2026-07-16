from dataclasses import FrozenInstanceError
from datetime import timezone
from decimal import Decimal
import hashlib

import pytest

from marketing_intelligence.page_content import (
    PagePrice,
    extract_page_data,
    normalize_main_text,
)


def test_extracts_present_missing_and_empty_metadata() -> None:
    present = extract_page_data(
        """
        <title>  Заголовок  </title>
        <meta NAME="DeScRiPtIoN" content="  Описание  ">
        <h1>  Первый H1  </h1><h1>Второй</h1>
        """,
        (),
    )
    missing = extract_page_data("<p>Текст</p>", ())
    empty = extract_page_data(
        '<title></title><meta name="description"><h1> </h1>',
        (),
    )

    assert (present.title, present.description, present.h1) == (
        "Заголовок",
        "Описание",
        "Первый H1",
    )
    assert (missing.title, missing.description, missing.h1) == (None, None, None)
    assert (empty.title, empty.description, empty.h1) == ("", "", "")


def test_metadata_uses_nfc_and_readable_whitespace_only() -> None:
    data = extract_page_data(
        """
        <title>  Е\u0308ЖИК\n—  тест! </title>
        <meta name="DESCRIPTION" content="  A\t B?  ">
        <h1>  H1\r\n Значение. </h1>
        """,
        (),
    )

    assert data.title == "ЁЖИК — тест!"
    assert data.description == "A B?"
    assert data.h1 == "H1 Значение."


def test_main_text_normalization_order_is_exact() -> None:
    value = "  Е\u0308ЖИК, Straße™\nслово—слово  е ё  "

    assert normalize_main_text(value) == "ёжик strasse слово слово е ё"


def test_excludes_approved_tags_roles_hidden_content_and_comments() -> None:
    tags = (
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
    )
    roles = (
        "navigation",
        "banner",
        "contentinfo",
        "complementary",
        "form",
        "dialog",
    )
    html = "<body><p>Оставить</p>"
    html += "".join(
        '<input value="метка-input">'
        if tag == "input"
        else f"<{tag}>метка-{tag}</{tag}>"
        for tag in tags
    )
    html += "".join(f'<div role="{role}">роль-{role}</div>' for role in roles)
    html += '<div hidden>скрыто</div><div aria-hidden=" TRUE ">невидимо</div>'
    html += "<!-- комментарий --><p>Конец</p></body>"

    data = extract_page_data(html, ())

    assert data.normalized_text == "оставить конец"


def test_keeps_image_alt_and_uses_body_before_document() -> None:
    data = extract_page_data(
        "<html><head><title>Не основной</title></head>"
        '<body>До<img alt="Важное изображение">После<img src="empty"></body></html>',
        (),
    )
    fallback = extract_page_data(
        '<title>Заголовок</title><main><img alt="Описание"> Текст</main>',
        (),
    )

    assert data.normalized_text == "до важное изображение после"
    assert fallback.normalized_text == "заголовок описание текст"


def test_hash_is_stable_for_text_and_empty_text() -> None:
    first = extract_page_data("<body>Привет, МИР!</body>", ())
    second = extract_page_data("<body> привет — мир </body>", ())
    empty = extract_page_data("<body><script>текст</script></body>", ())

    expected = hashlib.sha256("привет мир".encode()).hexdigest()
    assert first.content_hash == second.content_hash == expected
    assert empty.content_hash == hashlib.sha256(b"").hexdigest()
    assert len(first.content_hash) == 64
    assert first.content_hash == first.content_hash.lower()


def test_malformed_html_is_parsed_and_data_is_immutable() -> None:
    data = extract_page_data("<html><body><h1>Заголовок<p>Текст", ())

    assert data.h1 == "Заголовок"
    assert data.normalized_text == "заголовок текст"
    with pytest.raises(FrozenInstanceError):
        data.title = "Другое"  # type: ignore[misc]


def test_checked_at_is_timezone_aware_utc_and_links_are_passed_through() -> None:
    links = ("https://example.com/a", "https://example.com/b")

    data = extract_page_data("<p>Текст</p>", links)

    assert data.checked_at.tzinfo is timezone.utc
    assert data.checked_at.utcoffset().total_seconds() == 0
    assert data.internal_links is links


def test_extracts_json_ld_offer_graph_arrays_and_nested_offers() -> None:
    data = extract_page_data(
        """
        <script type="application/ld+json">
        {"@graph": [
          {"@type": "Product", "offers": [
            {"@type": "Offer", "price": 0, "priceCurrency": "rub"},
            {"@type": "Offer", "price": "19,90"}
          ]},
          {"@type": "AggregateOffer", "lowPrice": "10.00",
           "highPrice": 25, "priceCurrency": "usd"}
        ]}
        </script>
        """,
        (),
    )

    assert data.prices == (
        PagePrice(Decimal("0"), "RUB", "price", "json-ld"),
        PagePrice(Decimal("19.90"), None, "price", "json-ld"),
        PagePrice(Decimal("10.00"), "USD", "low", "json-ld"),
        PagePrice(Decimal("25"), "USD", "high", "json-ld"),
    )


def test_offer_price_wins_over_active_unit_price_specification() -> None:
    data = extract_page_data(
        """
        <script type="application/ld+json">
        {"@type": "Offer", "price": "100", "priceCurrency": "RUB",
         "priceSpecification":
           {"price": "90", "priceCurrency": "RUB"}}
        </script>
        <div itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="price" content="200">
          <meta itemprop="priceCurrency" content="usd">
          <div itemprop="priceSpecification" itemscope>
            <meta itemprop="price" content="180">
          </div>
        </div>
        """,
        (),
    )

    assert data.prices == (
        PagePrice(Decimal("100"), "RUB", "price", "json-ld"),
        PagePrice(Decimal("200"), "USD", "price", "microdata"),
    )


def test_extracts_active_unit_price_and_ignores_typed_old_price() -> None:
    data = extract_page_data(
        """
        <script type="application/ld+json">
        [
          {"@type": "UnitPriceSpecification", "price": "75",
           "priceCurrency": "eur"},
          {"@type": "UnitPriceSpecification", "price": "99",
           "priceType": "https://schema.org/StrikethroughPrice"}
        ]
        </script>
        <div itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="priceCurrency" content="rub">
          <div itemprop="priceSpecification" itemscope
               itemtype="https://schema.org/UnitPriceSpecification">
            <span itemprop="price" content="45.50">1 000 ₽</span>
          </div>
        </div>
        <div itemscope itemtype="https://schema.org/UnitPriceSpecification">
          <meta itemprop="price" content="60">
          <meta itemprop="priceType" content="StrikethroughPrice">
        </div>
        """,
        (),
    )

    assert data.prices == (
        PagePrice(Decimal("75"), "EUR", "price", "json-ld"),
        PagePrice(Decimal("45.50"), "RUB", "price", "microdata"),
    )


def test_microdata_range_prefers_content_and_matches_json_ld_kinds() -> None:
    data = extract_page_data(
        """
        <div itemscope itemtype="https://schema.org/AggregateOffer">
          <span itemprop="lowPrice" content="10,5">999</span>
          <meta itemprop="highPrice" content="20">
          <meta itemprop="priceCurrency" content="gbp">
        </div>
        """,
        (),
    )

    assert data.prices == (
        PagePrice(Decimal("10.5"), "GBP", "low", "microdata"),
        PagePrice(Decimal("20"), "GBP", "high", "microdata"),
    )


def test_deduplicates_exact_prices_preserving_first_source_and_order() -> None:
    data = extract_page_data(
        """
        <script type="application/ld+json">
        [{"@type":"Offer","price":"10.0","priceCurrency":"RUB"},
         {"@type":"Offer","price":"20","priceCurrency":"RUB"}]
        </script>
        <div itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="price" content="10.00">
          <meta itemprop="priceCurrency" content="rub">
        </div>
        """,
        (),
    )

    assert data.prices == (
        PagePrice(Decimal("10.0"), "RUB", "price", "json-ld"),
        PagePrice(Decimal("20"), "RUB", "price", "json-ld"),
    )


def test_ignores_ambiguous_invalid_and_malformed_values_safely() -> None:
    data = extract_page_data(
        """
        <title>Прежние данные</title><p>Цена 777 ₽</p>
        <script type="application/ld+json">{"broken":</script>
        <script type="application/ld+json">
        [{"@type":"Offer","price":"1,234,56","priceCurrency":"₽"},
         {"@type":"Offer","price":"-1"},
         {"@type":"Offer","price":"NaN"},
         {"@type":"Offer","price":"1e3"}]
        </script>
        """,
        (),
    )

    assert data.prices == ()
    assert data.title == "Прежние данные"
    assert data.normalized_text == "цена 777"


def test_page_price_is_frozen_and_page_without_schema_has_empty_tuple() -> None:
    data = extract_page_data("<p>Обычная страница</p>", ())
    price = PagePrice(Decimal("1"), None, "price", "json-ld")

    assert data.prices == ()
    with pytest.raises(FrozenInstanceError):
        price.amount = Decimal("2")  # type: ignore[misc]


def test_excessively_nested_json_ld_is_ignored_without_partial_prices() -> None:
    nested = '{"next":' * 2000 + "null" + "}" * 2000
    html = (
        "<title>Рабочая страница</title><p>Основной текст</p>"
        '<script type="application/ld+json">'
        '[{"@type":"Offer","price":"10","priceCurrency":"RUB"},'
        f"{nested}]</script>"
    )

    data = extract_page_data(html, ())

    assert data.prices == ()
    assert data.title == "Рабочая страница"
    assert data.normalized_text == "основной текст"
