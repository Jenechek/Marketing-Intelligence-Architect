"""Проверка фильтров и пагинации карты структуры."""

from dataclasses import dataclass
from urllib.parse import urlencode

from .site_structure import OUTCOME_TITLES, StructurePage


PAGES_PER_PAGE = 20
BOOLEAN_FILTERS = {"", "yes", "no"}


@dataclass(frozen=True, slots=True)
class StructureFilterState:
    url_value: str
    depth_value: str
    outcome_value: str
    broken_value: str
    unchecked_value: str
    page_value: str
    depth: int | None
    outcome: str | None
    broken: bool | None
    unchecked: bool | None
    page: int

    @property
    def has_filters(self) -> bool:
        return bool(self.url_value or self.depth_value or self.outcome_value or self.broken_value or self.unchecked_value)

    def query(self, *, page: int | None = None) -> str:
        values = [
            ("url", self.url_value),
            ("depth", self.depth_value),
            ("outcome", self.outcome_value),
            ("broken", self.broken_value),
            ("unchecked", self.unchecked_value),
        ]
        target_page = self.page if page is None else page
        if target_page != 1:
            values.append(("page", str(target_page)))
        return urlencode([(key, value) for key, value in values if value])


def parse_structure_filters(
    *, url: str, depth: str, outcome: str, broken: str, unchecked: str, page: str
) -> tuple[StructureFilterState | None, dict[str, str]]:
    errors: dict[str, str] = {}
    if len(url) > 2048 or any(ord(character) < 32 for character in url):
        errors["url"] = "Подстрока URL слишком длинная или содержит служебные символы."
    parsed_depth: int | None = None
    if depth:
        try:
            parsed_depth = int(depth)
            if str(parsed_depth) != depth or parsed_depth < 0:
                raise ValueError
        except ValueError:
            errors["depth"] = "Глубина должна быть целым неотрицательным числом."
    parsed_outcome = outcome or None
    if parsed_outcome is not None and parsed_outcome not in OUTCOME_TITLES:
        errors["outcome"] = "Выберите один из доступных результатов обработки."
    if broken not in BOOLEAN_FILTERS:
        errors["broken"] = "Выберите допустимое состояние битых ссылок."
    if unchecked not in BOOLEAN_FILTERS:
        errors["unchecked"] = "Выберите допустимое состояние непроверенных ссылок."
    try:
        parsed_page = int(page)
        if str(parsed_page) != page or parsed_page < 1:
            raise ValueError
    except ValueError:
        parsed_page = 1
        errors["page"] = "Номер страницы должен быть положительным целым числом."
    if errors:
        return None, errors
    return StructureFilterState(
        url, depth, outcome, broken, unchecked, page, parsed_depth, parsed_outcome,
        None if not broken else broken == "yes",
        None if not unchecked else unchecked == "yes",
        parsed_page,
    ), {}


def filter_structure_pages(
    pages: tuple[StructurePage, ...], state: StructureFilterState
) -> tuple[StructurePage, ...]:
    needle = state.url_value.casefold()
    return tuple(
        page for page in pages
        if (not needle or needle in page.url.casefold())
        and (state.depth is None or page.depth == state.depth)
        and (state.outcome is None or page.outcome == state.outcome)
        and (state.broken is None or (page.broken_outgoing_count > 0) is state.broken)
        and (state.unchecked is None or (page.unchecked_outgoing_count > 0) is state.unchecked)
    )


def structure_url(site_id: int, state: StructureFilterState, *, page: int | None = None) -> str:
    query = state.query(page=page)
    base = f"/sites/{site_id}/structure"
    return f"{base}?{query}" if query else base
