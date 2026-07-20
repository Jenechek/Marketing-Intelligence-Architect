"""Безопасная подготовка визуального графа и внешних ссылок карты."""

from dataclasses import dataclass
import math
from urllib.parse import urlsplit

from .site_structure import StructureEdge, StructurePage


@dataclass(frozen=True, slots=True)
class GraphNode:
    record_id: int
    number: int
    url: str
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class GraphLine:
    source_number: int
    target_number: int
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True, slots=True)
class GraphView:
    nodes: tuple[GraphNode, ...]
    lines: tuple[GraphLine, ...]


def build_graph_view(
    pages: tuple[StructurePage, ...], edges: tuple[StructureEdge, ...]
) -> GraphView:
    """Разместить до 100 узлов детерминированно по окружности."""

    if len(pages) > 100:
        raise ValueError("Граф не может содержать больше 100 узлов.")
    count = len(pages)
    nodes: list[GraphNode] = []
    for index, page in enumerate(pages, start=1):
        angle = -math.pi / 2 + (2 * math.pi * (index - 1) / max(count, 1))
        radius = 285 if count > 1 else 0
        nodes.append(
            GraphNode(
                page.record_id,
                index,
                page.url,
                round(450 + radius * math.cos(angle), 2),
                round(330 + radius * math.sin(angle), 2),
            )
        )
    by_id = {node.record_id: node for node in nodes}
    lines = tuple(
        GraphLine(
            by_id[edge.source_record_id].number,
            by_id[edge.target_record_id].number,
            by_id[edge.source_record_id].x,
            by_id[edge.source_record_id].y,
            by_id[edge.target_record_id].x,
            by_id[edge.target_record_id].y,
        )
        for edge in edges
        if edge.source_record_id != edge.target_record_id
    )
    return GraphView(tuple(nodes), lines)


def safe_external_url(value: str) -> str | None:
    """Разрешить открытие только обычного HTTP(S)-адреса без учётных данных."""

    if any(ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return value
