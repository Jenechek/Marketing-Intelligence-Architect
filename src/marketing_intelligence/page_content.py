"""Детерминированное извлечение содержимого HTML-страницы."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
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


def extract_page_data(html: str, internal_links: tuple[str, ...]) -> PageData:
    """Извлечь данные страницы без сети, хранения и поиска ссылок."""

    soup = BeautifulSoup(html, "lxml")
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
    )


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
