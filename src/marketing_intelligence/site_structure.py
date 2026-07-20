"""Нейтральная модель и графовый анализ одного сохранённого обхода."""

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import math


class StructureDataError(ValueError):
    """Сохранённые связанные данные карты противоречат друг другу."""


class StructureAnalysisError(RuntimeError):
    """PageRank нельзя достоверно рассчитать для переданного графа."""


class LinkState(StrEnum):
    AVAILABLE = "available"
    BROKEN = "broken"
    UNCHECKED = "unchecked"
    FORBIDDEN = "forbidden"
    REDIRECT = "redirect"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    OTHER = "other"


class StructuralSignal(StrEnum):
    LOW_CONNECTIVITY = "low_connectivity"
    DEAD_END = "dead_end"
    ISLAND = "island"
    CYCLE_TRAP = "cycle_trap"
    ANOMALOUS_DEPTH = "anomalous_depth"
    BOTTLENECK = "bottleneck"


SIGNAL_TITLES = {
    StructuralSignal.LOW_CONNECTIVITY: "Недостаточная внутренняя связность",
    StructuralSignal.DEAD_END: "Тупиковая зона",
    StructuralSignal.ISLAND: "Структурный остров",
    StructuralSignal.CYCLE_TRAP: "Циклическая ловушка",
    StructuralSignal.ANOMALOUS_DEPTH: "Аномальная глубина",
    StructuralSignal.BOTTLENECK: "Бутылочное горлышко",
}


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
class PageStructureAnalysis:
    record_id: int
    pagerank: float | None
    pagerank_rank: int | None
    signals: tuple[StructuralSignal, ...]
    dead_end_is_page: bool
    cycle_component_record_ids: tuple[int, ...]
    bottleneck_affected_count: int

    def has_signal(self, signal: StructuralSignal) -> bool:
        return signal in self.signals


@dataclass(frozen=True, slots=True)
class StructureAnalysis:
    pages: tuple[PageStructureAnalysis, ...]
    html_page_count: int
    pagerank_error: str | None
    low_connectivity_applicable: bool
    anomalous_depth_applicable: bool

    def page_by_id(self, record_id: int) -> PageStructureAnalysis | None:
        return next((page for page in self.pages if page.record_id == record_id), None)

    def signal_count(self, signal: StructuralSignal) -> int:
        return sum(page.has_signal(signal) for page in self.pages)


@dataclass(frozen=True, slots=True)
class SiteStructure:
    pages: tuple[StructurePage, ...]
    edges: tuple[StructureEdge, ...]
    analysis: StructureAnalysis

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
    built_pages = tuple(pages)
    built_edges = tuple(edges)
    return SiteStructure(built_pages, built_edges, analyze_site_structure(built_pages, built_edges))


def calculate_pagerank(
    node_ids: tuple[int, ...],
    adjacency: dict[int, tuple[int, ...]],
    *,
    damping: float = 0.85,
    tolerance: float = 1e-12,
    max_iterations: int = 200,
) -> dict[int, float]:
    """Рассчитать детерминированный направленный PageRank без зависимостей."""

    if not node_ids:
        return {}
    if (
        len(set(node_ids)) != len(node_ids)
        or not 0.0 < damping < 1.0
        or tolerance < 0.0
        or max_iterations < 1
    ):
        raise StructureAnalysisError("Параметры расчёта PageRank повреждены.")
    known = set(node_ids)
    normalized: dict[int, tuple[int, ...]] = {}
    for node_id in node_ids:
        targets = adjacency.get(node_id, ())
        if any(target not in known for target in targets):
            raise StructureAnalysisError("Граф PageRank содержит неизвестную страницу.")
        normalized[node_id] = tuple(sorted(set(targets)))

    count = len(node_ids)
    ranks = {node_id: 1.0 / count for node_id in node_ids}
    base = (1.0 - damping) / count
    for _ in range(max_iterations):
        dangling = sum(ranks[node_id] for node_id in node_ids if not normalized[node_id])
        updated = {node_id: base + damping * dangling / count for node_id in node_ids}
        for source_id in node_ids:
            targets = normalized[source_id]
            if targets:
                share = damping * ranks[source_id] / len(targets)
                for target_id in targets:
                    updated[target_id] += share
        if any(not math.isfinite(value) or value < 0.0 for value in updated.values()):
            raise StructureAnalysisError("Расчёт PageRank дал некорректное значение.")
        change = sum(abs(updated[node_id] - ranks[node_id]) for node_id in node_ids)
        ranks = updated
        if change <= tolerance:
            return ranks
    raise StructureAnalysisError("PageRank не сошёлся за 200 итераций.")


def analyze_site_structure(
    pages: tuple[StructurePage, ...], edges: tuple[StructureEdge, ...]
) -> StructureAnalysis:
    """Рассчитать PageRank и объяснимые сигналы только по HTML-графу."""

    html_pages = tuple(page for page in pages if page.outcome == "html")
    html_ids = tuple(page.record_id for page in html_pages)
    html_position = {record_id: index for index, record_id in enumerate(html_ids)}
    html_set = set(html_ids)
    adjacency_sets = {record_id: set() for record_id in html_ids}
    reverse_sets = {record_id: set() for record_id in html_ids}
    for edge in edges:
        if edge.source_record_id in html_set and edge.target_record_id in html_set:
            adjacency_sets[edge.source_record_id].add(edge.target_record_id)
            reverse_sets[edge.target_record_id].add(edge.source_record_id)
    adjacency = {
        record_id: tuple(sorted(targets))
        for record_id, targets in adjacency_sets.items()
    }

    pagerank_error: str | None = None
    try:
        pageranks = calculate_pagerank(html_ids, adjacency)
    except StructureAnalysisError as error:
        pageranks = {}
        pagerank_error = str(error)
    ranked_ids = sorted(
        html_ids, key=lambda item: (-pageranks.get(item, 0.0), html_position[item])
    )
    ranks = {record_id: index for index, record_id in enumerate(ranked_ids, start=1)} if pageranks else {}

    start_id = pages[0].record_id if pages else None
    reachable = _reachable(start_id, adjacency) if start_id in html_set else set()
    islands = html_set - reachable
    components = _strongly_connected_components(html_ids, adjacency)
    sink_components = tuple(
        component
        for component in components
        if not any(
            target not in component
            for source in component
            for target in adjacency[source]
        )
    )
    dead_end_ids = set().union(*sink_components) if sink_components else set()
    cycle_components = tuple(
        component
        for component in sink_components
        if len(component) >= 2 or any(node in adjacency[node] for node in component)
    )
    cycle_by_id = {
        node: tuple(sorted(component, key=html_position.__getitem__))
        for component in cycle_components
        for node in component
    }

    low_candidates = tuple(
        record_id for record_id in html_ids
        if record_id != start_id and record_id not in islands
    )
    low_applicable = len(low_candidates) >= 10 and not pagerank_error
    low_ids: set[int] = set()
    if low_applicable:
        p20 = _nearest_rank(tuple(sorted(pageranks[item] for item in low_candidates)), 0.20)
        low_ids = {
            item for item in low_candidates
            if pageranks[item] < p20 and len(reverse_sets[item]) <= 1
        }

    reachable_ids = tuple(item for item in html_ids if item in reachable)
    depth_applicable = len(reachable_ids) >= 10
    deep_ids: set[int] = set()
    if depth_applicable:
        page_by_id = {page.record_id: page for page in html_pages}
        p90 = _nearest_rank(tuple(sorted(page_by_id[item].depth for item in reachable_ids)), 0.90)
        deep_ids = {
            item for item in reachable_ids
            if page_by_id[item].depth >= 3 and page_by_id[item].depth > p90
        }

    bottlenecks: dict[int, int] = {}
    if start_id in reachable:
        threshold = max(2, math.ceil(0.10 * (len(reachable) - 1)))
        for removed in reachable_ids:
            if removed == start_id:
                continue
            remaining_reachable = _reachable(start_id, adjacency, removed=removed)
            affected = len(reachable - {removed} - remaining_reachable)
            if affected >= threshold:
                bottlenecks[removed] = affected

    result: list[PageStructureAnalysis] = []
    signal_order = tuple(StructuralSignal)
    for page in pages:
        record_id = page.record_id
        signals: set[StructuralSignal] = set()
        if record_id in low_ids:
            signals.add(StructuralSignal.LOW_CONNECTIVITY)
        if record_id in dead_end_ids:
            signals.add(StructuralSignal.DEAD_END)
        if record_id in islands and record_id != start_id:
            signals.add(StructuralSignal.ISLAND)
        if record_id in cycle_by_id:
            signals.add(StructuralSignal.CYCLE_TRAP)
        if record_id in deep_ids:
            signals.add(StructuralSignal.ANOMALOUS_DEPTH)
        if record_id in bottlenecks:
            signals.add(StructuralSignal.BOTTLENECK)
        result.append(
            PageStructureAnalysis(
                record_id,
                pageranks.get(record_id),
                ranks.get(record_id),
                tuple(signal for signal in signal_order if signal in signals),
                record_id in dead_end_ids and not adjacency.get(record_id, ()),
                cycle_by_id.get(record_id, ()),
                bottlenecks.get(record_id, 0),
            )
        )
    return StructureAnalysis(
        tuple(result), len(html_ids), pagerank_error, low_applicable, depth_applicable
    )


def _nearest_rank(values: tuple[float | int, ...], percentile: float) -> float | int:
    return values[math.ceil(percentile * len(values)) - 1]


def _reachable(
    start_id: int | None,
    adjacency: dict[int, tuple[int, ...]],
    *,
    removed: int | None = None,
) -> set[int]:
    if start_id is None or start_id == removed or start_id not in adjacency:
        return set()
    found = {start_id}
    pending = deque([start_id])
    while pending:
        source = pending.popleft()
        for target in adjacency[source]:
            if target != removed and target not in found:
                found.add(target)
                pending.append(target)
    return found


def _strongly_connected_components(
    node_ids: tuple[int, ...], adjacency: dict[int, tuple[int, ...]]
) -> tuple[frozenset[int], ...]:
    """Детерминированный алгоритм Тарьяна."""

    next_index = 0
    indexes: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[frozenset[int]] = []

    def visit(node: int) -> None:
        nonlocal next_index
        indexes[node] = next_index
        lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)
        for target in adjacency[node]:
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component: set[int] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            components.append(frozenset(component))

    for node in node_ids:
        if node not in indexes:
            visit(node)
    return tuple(components)


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
