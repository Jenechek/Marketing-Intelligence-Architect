import math

import pytest

from marketing_intelligence.site_structure import (
    RawStructurePage,
    StructuralSignal,
    StructureAnalysisError,
    build_site_structure,
    calculate_pagerank,
)


def raw(
    record_id: int,
    sequence: int,
    url: str,
    *,
    depth: int = 0,
    outcome: str = "html",
    links: tuple[str, ...] = (),
) -> RawStructurePage:
    return RawStructurePage(
        record_id=record_id,
        sequence_number=sequence,
        url=url,
        depth=depth,
        outcome=outcome,
        message=outcome,
        http_status=200,
        internal_links=links if outcome == "html" else None,
    )


def test_pagerank_handles_cycle_chain_branch_and_dangling_nodes_deterministically():
    cycle = calculate_pagerank((1, 2, 3), {1: (2,), 2: (3,), 3: (1,)})
    assert cycle == pytest.approx({1: 1 / 3, 2: 1 / 3, 3: 1 / 3})

    graph = {1: (2, 3), 2: (3,), 3: (), 4: (3,)}
    first = calculate_pagerank((1, 2, 3, 4), graph)
    second = calculate_pagerank((1, 2, 3, 4), graph)
    assert first == second
    assert sum(first.values()) == pytest.approx(1.0)
    assert first[3] > first[2] > first[1]
    assert first[1] == pytest.approx(first[4])


def test_pagerank_reports_controlled_validation_and_convergence_errors():
    with pytest.raises(StructureAnalysisError, match="неизвестную страницу"):
        calculate_pagerank((1,), {1: (2,)})
    with pytest.raises(StructureAnalysisError, match="не сошёлся"):
        calculate_pagerank((1, 2), {1: (2,), 2: ()}, tolerance=0, max_iterations=1)


def test_structural_signals_distinguish_sink_island_trap_exit_and_bottleneck():
    base = "https://signals.test/"
    structure = build_site_structure(
        (
            raw(1, 1, base, links=(base + "a",)),
            raw(2, 2, base + "a", depth=1, links=(base + "b", base + "exit")),
            raw(3, 3, base + "b", depth=2, links=(base + "a",)),
            raw(4, 4, base + "exit", depth=2),
            raw(5, 5, base + "island-a", links=(base + "island-b",)),
            raw(6, 6, base + "island-b", links=(base + "island-a",)),
            raw(7, 7, base + "asset", outcome="non_html"),
        )
    )
    analysis = {item.record_id: item for item in structure.analysis.pages}

    assert StructuralSignal.CYCLE_TRAP not in analysis[2].signals
    assert StructuralSignal.DEAD_END not in analysis[2].signals
    assert analysis[4].dead_end_is_page
    assert StructuralSignal.DEAD_END in analysis[4].signals
    assert StructuralSignal.ISLAND in analysis[5].signals
    assert StructuralSignal.CYCLE_TRAP in analysis[5].signals
    assert analysis[5].cycle_component_record_ids == (5, 6)
    assert StructuralSignal.BOTTLENECK in analysis[2].signals
    assert analysis[2].bottleneck_affected_count == 2
    assert not analysis[1].has_signal(StructuralSignal.ISLAND)
    assert not analysis[1].has_signal(StructuralSignal.BOTTLENECK)
    assert analysis[7].pagerank is None
    assert analysis[7].signals == ()
    assert structure.analysis.html_page_count == 6


def test_equal_pagerank_uses_strict_p20_and_small_graph_has_no_low_signal():
    base = "https://equal.test/"
    pages = tuple(
        raw(
            number,
            number,
            base + str(number),
            links=(base + str(number % 11 + 1),),
        )
        for number in range(1, 12)
    )
    structure = build_site_structure(pages)
    assert structure.analysis.low_connectivity_applicable
    assert not any(
        item.has_signal(StructuralSignal.LOW_CONNECTIVITY)
        for item in structure.analysis.pages
    )

    small = build_site_structure(pages[:9])
    assert not small.analysis.low_connectivity_applicable
    assert all(item.pagerank is not None for item in small.analysis.pages)


def test_low_connectivity_flags_only_strictly_low_rank_with_at_most_one_incoming():
    base = "https://low.test/"
    pages = [raw(1, 1, base + "start", links=(base + "2", base + "11"))]
    for number in range(2, 11):
        target = 2 if number == 10 else number + 1
        pages.append(
            raw(number, number, base + str(number), links=(base + str(target),))
        )
    pages.append(raw(11, 11, base + "11"))
    structure = build_site_structure(tuple(pages))
    analysis = {item.record_id: item for item in structure.analysis.pages}
    assert structure.analysis.low_connectivity_applicable
    assert analysis[11].has_signal(StructuralSignal.LOW_CONNECTIVITY)
    assert not analysis[1].has_signal(StructuralSignal.LOW_CONNECTIVITY)


def test_self_link_without_component_exit_is_a_cycle_trap():
    base = "https://self.test/"
    structure = build_site_structure(
        (
            raw(1, 1, base, links=(base + "self",)),
            raw(2, 2, base + "self", links=(base + "self",)),
        )
    )
    page = structure.analysis.page_by_id(2)
    assert page is not None
    assert page.has_signal(StructuralSignal.CYCLE_TRAP)
    assert page.cycle_component_record_ids == (2,)


def test_anomalous_depth_uses_strict_nearest_rank_p90():
    base = "https://depth.test/"
    depths = (0, 1, 2, 3, 4, 5, 6, 7, 8, 10)
    pages = []
    for index, depth in enumerate(depths, start=1):
        links = (base + str(index + 1),) if index < len(depths) else ()
        pages.append(raw(index, index, base + str(index), depth=depth, links=links))
    structure = build_site_structure(tuple(pages))
    analysis = {item.record_id: item for item in structure.analysis.pages}
    assert structure.analysis.anomalous_depth_applicable
    assert not analysis[9].has_signal(StructuralSignal.ANOMALOUS_DEPTH)
    assert analysis[10].has_signal(StructuralSignal.ANOMALOUS_DEPTH)


def test_pagerank_rank_is_stable_for_equal_values():
    base = "https://rank.test/"
    structure = build_site_structure(
        (
            raw(10, 1, base + "first", links=(base + "second",)),
            raw(5, 2, base + "second", links=(base + "first",)),
        )
    )
    analyses = structure.analysis.pages
    assert math.isclose(analyses[0].pagerank, analyses[1].pagerank)
    assert [item.pagerank_rank for item in analyses] == [1, 2]
