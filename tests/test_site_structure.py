import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session

from marketing_intelligence.config import Settings
from marketing_intelligence.crawl_history import execute_crawl_run, start_crawl_run
from marketing_intelligence.crawler import CrawlSettings, Crawler
from marketing_intelligence.main import create_app
from marketing_intelligence.models import CrawlPageRecord, CrawlPageSnapshot, CrawlRun, Site
from marketing_intelligence.site_structure import (
    LinkState,
    RawStructurePage,
    build_site_structure,
)
from marketing_intelligence.site_structure_query import load_site_structure


def build_app(tmp_path: Path):
    database = tmp_path / "data" / "test.db"
    settings = Settings(
        data_dir=database.parent,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database.as_posix()}",
    )
    return create_app(settings), database


def seed_site(session: Session, *, name: str = "Карта", url: str = "https://example.test/") -> Site:
    site = Site(name=name, url=url)
    session.add(site)
    session.flush()
    return site


def seed_run(
    session: Session,
    site_id: int,
    *,
    status: str = "completed",
    completed_at: datetime | None = None,
    limited: bool = False,
) -> CrawlRun:
    completed = completed_at or datetime.now(UTC)
    run = CrawlRun(
        site_id=site_id,
        started_at=completed - timedelta(minutes=1),
        completed_at=completed,
        status=status,
        message="Готово",
        max_pages=200,
        max_depth=3,
        delay=0,
        timeout=1,
        user_agent="test",
        processed=0,
        requested=0,
        successful=0,
        forbidden=0,
        errors=0,
        limited=limited,
    )
    session.add(run)
    session.flush()
    return run


def seed_page(
    session: Session,
    run_id: int,
    sequence: int,
    url: str,
    *,
    depth: int = 0,
    outcome: str = "html",
    http_status: int | None = 200,
    links: list[str] | None = None,
) -> CrawlPageRecord:
    page = CrawlPageRecord(
        crawl_run_id=run_id,
        sequence_number=sequence,
        url=url,
        depth=depth,
        outcome=outcome,
        message=f"Результат {outcome}",
        http_status=http_status,
    )
    session.add(page)
    session.flush()
    if outcome == "html":
        session.add(
            CrawlPageSnapshot(
                crawl_page_record_id=page.id,
                checked_at=datetime.now(UTC),
                title=None,
                description=None,
                h1=None,
                normalized_text="",
                content_hash="0" * 64,
                internal_links_json=json.dumps(links or [], ensure_ascii=False),
            )
        )
    return page


def raw(
    record_id: int,
    sequence: int,
    url: str,
    *,
    outcome: str = "html",
    status: int | None = 200,
    links: tuple[str, ...] | None = (),
) -> RawStructurePage:
    return RawStructurePage(
        record_id, sequence, url, sequence - 1, outcome, outcome, status,
        links if outcome == "html" else None,
    )


def test_domain_deduplicates_edges_and_classifies_only_confirmed_http_errors_as_broken():
    base = "https://example.test/"
    structure = build_site_structure(
        (
            raw(1, 1, base, links=(base + "broken", base + "broken", base + "missing", base + "forbidden", base + "redirect", base + "timeout", base + "network", base + "file")),
            raw(2, 2, base + "broken", outcome="http_error", status=404),
            raw(3, 3, base + "forbidden", outcome="forbidden", status=None),
            raw(4, 4, base + "redirect", outcome="redirect", status=301),
            raw(5, 5, base + "timeout", outcome="timeout", status=None),
            raw(6, 6, base + "network", outcome="network_error", status=None),
            raw(7, 7, base + "file", outcome="non_html", status=200),
        )
    )
    page = structure.pages[0]
    assert page.outgoing_count == 7
    assert page.broken_outgoing_count == 1
    assert page.unchecked_outgoing_count == 1
    assert [link.state for link in page.outgoing] == [
        LinkState.BROKEN,
        LinkState.UNCHECKED,
        LinkState.FORBIDDEN,
        LinkState.REDIRECT,
        LinkState.TIMEOUT,
        LinkState.NETWORK_ERROR,
        LinkState.AVAILABLE,
    ]
    assert structure.pages[1].incoming_count == 1
    assert len(structure.edges) == 6


def test_tree_uses_first_source_breaks_cycle_and_keeps_orphans_once():
    base = "https://tree.test/"
    structure = build_site_structure(
        (
            raw(1, 1, base, links=(base + "a", base + "b")),
            raw(2, 2, base + "a", links=(base + "target",)),
            raw(3, 3, base + "b", links=(base + "target",)),
            raw(4, 4, base + "target"),
            raw(5, 5, base + "cycle-a", links=(base + "cycle-b",)),
            raw(6, 6, base + "cycle-b", links=(base + "cycle-a",)),
            raw(7, 7, base + "orphan"),
        )
    )
    tree = structure.tree_for()
    assert tree.roots[0].page.record_id == 1
    node_a = tree.roots[0].children[0]
    assert node_a.page.record_id == 2
    assert node_a.children[0].page.record_id == 4
    assert [node.page.record_id for node in tree.orphan_roots] == [5, 7]

    def flatten(nodes):
        result = []
        for node in nodes:
            result.append(node.page.record_id)
            result.extend(flatten(node.children))
        return result

    shown = flatten(tree.roots) + flatten(tree.orphan_roots)
    assert sorted(shown) == list(range(1, 8))
    assert len(shown) == len(set(shown))


def test_query_selects_latest_eligible_run_and_isolates_sites_in_two_queries(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app):
        with Session(app.state.engine, expire_on_commit=False) as session:
            first = seed_site(session)
            other = seed_site(session, name="Другой", url="https://other.test/")
            old = seed_run(session, first.id, completed_at=datetime(2026, 1, 1, tzinfo=UTC))
            seed_page(session, old.id, 1, "https://example.test/old")
            running = seed_run(session, first.id, status="running", completed_at=datetime(2026, 3, 1, tzinfo=UTC))
            seed_page(session, running.id, 1, "https://example.test/running")
            latest = seed_run(session, first.id, status="partial", completed_at=datetime(2026, 2, 1, tzinfo=UTC))
            seed_page(session, latest.id, 1, "https://example.test/latest")
            foreign = seed_run(session, other.id, completed_at=datetime(2026, 4, 1, tzinfo=UTC))
            seed_page(session, foreign.id, 1, "https://other.test/foreign")
            session.commit()
        statements: list[str] = []
        listener = lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement)
        event.listen(app.state.engine, "before_cursor_execute", listener)
        try:
            selected = load_site_structure(app.state.engine, first.id)
        finally:
            event.remove(app.state.engine, "before_cursor_execute", listener)
        assert selected.run.id == latest.id
        assert [page.url for page in selected.structure.pages] == ["https://example.test/latest"]
        assert len(statements) == 2


def test_map_filters_paginates_details_and_preserves_return_state(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        with Session(app.state.engine, expire_on_commit=False) as session:
            site = seed_site(session)
            run = seed_run(session, site.id, status="partial", limited=True)
            start = seed_page(session, run.id, 1, "https://example.test/", links=["https://example.test/broken", "https://example.test/unseen"])
            broken = seed_page(session, run.id, 2, "https://example.test/broken", depth=1, outcome="http_error", http_status=500)
            for number in range(3, 25):
                seed_page(session, run.id, number, f"https://example.test/page-{number}", depth=1)
            session.commit()
        response = client.get("/sites/1/structure")
        assert response.status_code == 200
        assert "Карта может быть неполной" in response.text
        assert "Достигнуто" in response.text
        assert "Страница 1 из 2" in response.text
        assert 'href="/sites/1/structure"' in client.get("/").text
        assert 'href="/sites/1/structure"' in client.get(f"/crawl-runs/{run.id}").text
        filtered = client.get("/sites/1/structure?depth=0&broken=yes&page=1")
        assert filtered.status_code == 200
        assert "https://example.test/" in filtered.text
        assert "https://example.test/page-3" not in filtered.text
        detail = client.get(f"/sites/1/structure/pages/{start.id}?depth=0&broken=yes&page=1")
        assert detail.status_code == 200
        assert "Битая: получен HTTP-код 400–599" in detail.text
        assert "Не проверена из-за границ или ограничений обхода" in detail.text
        assert 'href="/sites/1/structure?depth=0&amp;broken=yes"' in detail.text
        incoming = client.get(f"/sites/1/structure/pages/{broken.id}?depth=0&broken=yes")
        assert "https://example.test/" in incoming.text
        assert client.get("/sites/1/structure?depth=-1").status_code == 422
        assert client.get("/sites/1/structure?outcome=unknown").status_code == 422
        assert client.get("/sites/1/structure?page=99").status_code == 422
        assert "По фильтру ничего не найдено" in client.get(
            "/sites/1/structure?url=absent"
        ).text


def test_graph_refuses_more_than_100_nodes_until_filter_is_applied(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        with Session(app.state.engine, expire_on_commit=False) as session:
            site = seed_site(session)
            run = seed_run(session, site.id)
            for number in range(101):
                seed_page(session, run.id, number + 1, f"https://large.test/node-{number}")
            session.commit()
        response = client.get("/sites/1/structure")
        assert "Отобрано 101 узлов" in response.text
        assert "<svg" not in response.text
        filtered = client.get("/sites/1/structure?url=node-100")
        assert filtered.status_code == 200
        assert "<svg" in filtered.text
        assert "https://large.test/node-100" in filtered.text


def test_empty_missing_corrupt_and_escaped_states(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        with Session(app.state.engine, expire_on_commit=False) as session:
            no_run = seed_site(session, name="Без обхода", url="https://none.test/")
            empty_site = seed_site(session, name="Пустой", url="https://empty.test/")
            seed_run(session, empty_site.id)
            corrupt_site = seed_site(session, name="Сломан", url="https://corrupt.test/")
            corrupt_run = seed_run(session, corrupt_site.id)
            seed_page(session, corrupt_run.id, 1, "https://corrupt.test/")
            unsafe = seed_site(session, name='<script>alert("x")</script>', url="https://safe.test/")
            unsafe_run = seed_run(session, unsafe.id)
            unsafe_page = seed_page(session, unsafe_run.id, 1, 'javascript:alert("x")')
            session.commit()
            snapshot = session.get(CrawlPageSnapshot, 1)
            snapshot.internal_links_json = "{"
            session.add(snapshot)
            session.commit()
        assert "Подходящего обхода пока нет" in client.get(f"/sites/{no_run.id}/structure").text
        assert "В обходе нет страниц" in client.get(f"/sites/{empty_site.id}/structure").text
        corrupt = client.get(f"/sites/{corrupt_site.id}/structure")
        assert corrupt.status_code == 500
        assert "Связанные данные выбранного обхода повреждены" in corrupt.text
        escaped = client.get(f"/sites/{unsafe.id}/structure/pages/{unsafe_page.id}")
        assert escaped.status_code == 200
        assert "<script>" not in escaped.text
        assert "javascript:alert" in escaped.text
        assert "Открыть URL" not in escaped.text


def test_all_map_reads_leave_sqlite_byte_identical(tmp_path: Path):
    app, database = build_app(tmp_path)
    with TestClient(app) as client:
        with Session(app.state.engine, expire_on_commit=False) as session:
            site = seed_site(session)
            run = seed_run(session, site.id)
            page = seed_page(session, run.id, 1, "https://example.test/")
            session.commit()
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        assert client.get("/sites/1/structure").status_code == 200
        assert client.get(f"/sites/1/structure/pages/{page.id}").status_code == 200
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        assert after == before


def test_loopback_crawl_builds_real_structure_with_proxies_cleared(tmp_path: Path, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /"
                content_type = "text/plain"
                status = 200
            elif self.path == "/":
                body = b'<a href="/ok">ok</a><a href="/broken">broken</a>'
                content_type = "text/html"
                status = 200
            elif self.path == "/ok":
                body = b"<h1>ok</h1>"
                content_type = "text/html"
                status = 200
            else:
                body = b"broken"
                content_type = "text/plain"
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}/"
    app, _ = build_app(tmp_path)
    try:
        with TestClient(app) as client:
            with Session(app.state.engine, expire_on_commit=False) as session:
                site = seed_site(session, url=root)
                session.commit()
            started = start_crawl_run(app.state.engine, site.id, CrawlSettings(max_pages=10, max_depth=2, delay=0, timeout=2))

            async def no_delay(_seconds: float):
                return None

            asyncio.run(execute_crawl_run(app.state.engine, started.id, root, crawler=Crawler(delay=no_delay)))
            response = client.get(f"/sites/{site.id}/structure")
            assert response.status_code == 200
            assert root + "ok" in response.text
            selected = load_site_structure(app.state.engine, site.id)
            detail = client.get(
                f"/sites/{site.id}/structure/pages/{selected.structure.pages[0].record_id}"
            )
            assert "Битая: получен HTTP-код 400–599" in detail.text
    finally:
        server.shutdown()
        thread.join(timeout=2)
