from dataclasses import FrozenInstanceError
from datetime import timezone
import hashlib

import pytest

from marketing_intelligence.page_content import (
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
