"""Нейтральные типы событий изменения снимков."""

from enum import Enum


class ChangeEventType(str, Enum):
    """Поддерживаемые отдельные события сравнения снимков."""

    PAGE_ADDED = "page_added"
    PAGE_REMOVED = "page_removed"
    TITLE_CHANGED = "title_changed"
    DESCRIPTION_CHANGED = "description_changed"
    H1_CHANGED = "h1_changed"
    TEXT_CHANGED = "text_changed"
    INTERNAL_LINKS_CHANGED = "internal_links_changed"
