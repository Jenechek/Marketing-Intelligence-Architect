"""Нейтральная модель карты структуры одного сохранённого обхода."""

from dataclasses import dataclass
from enum import StrEnum


class StructureDataError(ValueError):
    """Сохранённые связанные данные карты противоречат друг другу."""


class LinkState(StrEnum):
    AVAILABLE = "available"
    BROKEN = "broken"
    UNCHECKED = "unchecked"
    FORBIDDEN = "forbidden"
    REDIRECT = "redirect"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    OTHER = "other"


OUTCOME_TITLES = {
    "html": "HTML-страница доступна",
    "non_html": "Доступный non-HTML ресурс",
    "oversized": "HTML-страница превышает допустимый размер",
    "parse_error": "Ошибка разбора HTML",
    "forbidden": "Запрещено robots.txt",
    "redirect": "Перенаправление",
    "http_error": "Ошибка HTTP",
    "network_error": "Ошибка сети",
    "timeout": "Истекло время ожидания",
}

LINK_STATE_TITLES = {
    LinkState.AVAILABLE: "Доступна",
    LinkState.BROKEN: "Битая: получен HTTP-код 400–599",
    LinkState.UNCHECKED: "Не проверена из-за границ или ограничений обхода",
    LinkState.FORBIDDEN: "Запрещена robots.txt и не запрашивалась",
    LinkState.REDIRECT: "Перенаправление не выполнялось",
    LinkState.NETWORK_ERROR: "Ошибка сети",
    LinkState.TIMEOUT: "Истекло время ожидания",
    LinkState.OTHER: "Другой фактический результат",
}


@dataclass(frozen=True, slots=True)
class RawStructurePage:
    record_id: int
    sequence_number: int
    url: str
    depth: int
    outcome: str
    message: str
    http_status: int | None
    internal_links: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class OutgoingLink:
    url: str
    state: LinkState
    state_title: str
    target_record_id: int | None
    target_outcome: str | None
    http_status: int | None


@dataclass(frozen=True, slots=True)
class StructurePage:
    record_id: int
    sequence_number: int
    url: str
    depth: int
    outcome: str
    outcome_title: str
    message: str
    http_status: int | None
    outgoing: tuple[OutgoingLink, ...]
    incoming_record_ids: tuple[int, ...]
    outgoing_count: int
    incoming_count: int
    broken_outgoing_count: int
    unchecked_outgoing_count: int


@dataclass(frozen=True, slots=True)
class StructureEdge:
    source_record_id: int
    target_record_id: int


@dataclass(frozen=True, slots=True)
class TreeNode:
    page: StructurePage
    children: tuple["TreeNode", ...]


@dataclass(frozen=True, slots=True)
class StructureTree:
    roots: tuple[TreeNode, ...]
    orphan_roots: tuple[TreeNode, ...]


@dataclass(frozen=True, slots=True)
class SiteStructure:
    pages: tuple[StructurePage, ...]
    edges: tuple[StructureEdge, ...]

    def page_by_id(self, record_id: int) -> StructurePage | None:
        return next((page for page in self.pages if page.record_id == record_id), None)

    def tree_for(self, pages: tuple[StructurePage, ...] | None = None) -> StructureTree:
        return build_tree(self.pages if pages is None else pages)

    def edges_for(self, pages: tuple[StructurePage, ...]) -> tuple[StructureEdge, ...]:
        selected = {page.record_id for page in pages}
        return tuple(
            edge
            for edge in self.edges
            if edge.source_record_id in selected and edge.target_record_id in selected
        )


def build_site_structure(raw_pages: tuple[RawStructurePage, ...]) -> SiteStructure:
    """Вычислить уникальные рёбра, метрики и статусы без ORM или СУБД."""

    ordered = tuple(sorted(raw_pages, key=lambda item: (item.sequence_number, item.record_id)))
    record_ids: set[int] = set()
    sequences: set[int] = set()
    by_url: dict[str, RawStructurePage] = {}
    for page in ordered:
        if page.record_id < 1 or page.record_id in record_ids:
            raise StructureDataError("Идентификаторы записей страниц повреждены.")
        if page.sequence_number < 1 or page.sequence_number in sequences:
            raise StructureDataError("Порядок страниц выбранного обхода повреждён.")
        if not page.url or page.url in by_url:
            raise StructureDataError("URL страниц выбранного обхода повреждены или повторяются.")
        if page.depth < 0 or page.outcome not in OUTCOME_TITLES:
            raise StructureDataError("Метаданные страницы выбранного обхода повреждены.")
        if page.outcome == "html" and page.internal_links is None:
            raise StructureDataError("Для HTML-страницы отсутствует связанный снимок.")
        if page.outcome != "html" and page.internal_links is not None:
            raise StructureDataError("Снимок связан с результатом, который не является HTML.")
        record_ids.add(page.record_id)
        sequences.add(page.sequence_number)
        by_url[page.url] = page

    outgoing_by_source: dict[int, tuple[OutgoingLink, ...]] = {}
    incoming: dict[int, set[int]] = {page.record_id: set() for page in ordered}
    edges: list[StructureEdge] = []
    for source in ordered:
        unique_links = _unique_links(source.internal_links or ())
        outgoing: list[OutgoingLink] = []
        for url in unique_links:
            target = by_url.get(url)
            state = _link_state(target)
            outgoing.append(
                OutgoingLink(
                    url=url,
                    state=state,
                    state_title=_link_title(state, target),
                    target_record_id=target.record_id if target else None,
                    target_outcome=target.outcome if target else None,
                    http_status=target.http_status if target else None,
                )
            )
            if target is not None:
                edges.append(StructureEdge(source.record_id, target.record_id))
                incoming[target.record_id].add(source.record_id)
        outgoing_by_source[source.record_id] = tuple(outgoing)

    pages: list[StructurePage] = []
    sequence_by_id = {page.record_id: page.sequence_number for page in ordered}
    for raw in ordered:
        outgoing = outgoing_by_source[raw.record_id]
        incoming_ids = tuple(sorted(incoming[raw.record_id], key=sequence_by_id.__getitem__))
        pages.append(
            StructurePage(
                record_id=raw.record_id,
                sequence_number=raw.sequence_number,
                url=raw.url,
                depth=raw.depth,
                outcome=raw.outcome,
                outcome_title=OUTCOME_TITLES[raw.outcome],
                message=raw.message,
                http_status=raw.http_status,
                outgoing=outgoing,
                incoming_record_ids=incoming_ids,
                outgoing_count=len(outgoing),
                incoming_count=len(incoming_ids),
                broken_outgoing_count=sum(link.state is LinkState.BROKEN for link in outgoing),
                unchecked_outgoing_count=sum(link.state is LinkState.UNCHECKED for link in outgoing),
            )
        )
    return SiteStructure(tuple(pages), tuple(edges))


def build_tree(pages: tuple[StructurePage, ...]) -> StructureTree:
    """Выбрать основного родителя и безопасно разорвать возможные циклы."""

    if not pages:
        return StructureTree((), ())
    ordered = tuple(sorted(pages, key=lambda item: (item.sequence_number, item.record_id)))
    by_id = {page.record_id: page for page in ordered}
    sequence = {page.record_id: page.sequence_number for page in ordered}
    start_id = ordered[0].record_id
    candidates: dict[int, list[int]] = {page.record_id: [] for page in ordered}
    for source in ordered:
        for link in source.outgoing:
            if (
                link.target_record_id in by_id
                and link.target_record_id != source.record_id
                and link.target_record_id != start_id
            ):
                candidates[link.target_record_id].append(source.record_id)
    parents: dict[int, int | None] = {
        page.record_id: (
            min(candidates[page.record_id], key=sequence.__getitem__)
            if candidates[page.record_id]
            else None
        )
        for page in ordered
    }
    parents[start_id] = None
    _break_parent_cycles(parents, sequence)

    children: dict[int, list[int]] = {page.record_id: [] for page in ordered}
    for child_id, parent_id in parents.items():
        if parent_id is not None:
            children[parent_id].append(child_id)
    for values in children.values():
        values.sort(key=sequence.__getitem__)

    def node(record_id: int) -> TreeNode:
        return TreeNode(by_id[record_id], tuple(node(child) for child in children[record_id]))

    root = node(start_id)
    orphan_ids = sorted(
        (record_id for record_id, parent in parents.items() if parent is None and record_id != start_id),
        key=sequence.__getitem__,
    )
    return StructureTree((root,), tuple(node(record_id) for record_id in orphan_ids))


def _unique_links(links: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, str) or not link:
            raise StructureDataError("Список внутренних ссылок повреждён.")
        if link not in seen:
            seen.add(link)
            result.append(link)
    return tuple(result)


def _link_state(target: RawStructurePage | None) -> LinkState:
    if target is None:
        return LinkState.UNCHECKED
    if target.outcome in {"html", "non_html"}:
        return LinkState.AVAILABLE
    if target.outcome == "http_error" and target.http_status is not None and 400 <= target.http_status <= 599:
        return LinkState.BROKEN
    return {
        "forbidden": LinkState.FORBIDDEN,
        "redirect": LinkState.REDIRECT,
        "network_error": LinkState.NETWORK_ERROR,
        "timeout": LinkState.TIMEOUT,
    }.get(target.outcome, LinkState.OTHER)


def _link_title(state: LinkState, target: RawStructurePage | None) -> str:
    if state is LinkState.AVAILABLE and target is not None and target.outcome == "non_html":
        return "Доступна: корректный non-HTML ресурс"
    if state is LinkState.OTHER and target is not None:
        return f"Другой результат: {OUTCOME_TITLES[target.outcome]}"
    return LINK_STATE_TITLES[state]


def _break_parent_cycles(parents: dict[int, int | None], sequence: dict[int, int]) -> None:
    finished: set[int] = set()
    for origin in sorted(parents, key=sequence.__getitem__):
        path: list[int] = []
        positions: dict[int, int] = {}
        current: int | None = origin
        while current is not None and current not in finished:
            if current in positions:
                cycle = path[positions[current] :]
                parents[min(cycle, key=sequence.__getitem__)] = None
                break
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        finished.update(path)
