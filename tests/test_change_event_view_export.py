import asyncio
import csv
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
from sqlalchemy import event, inspect
from sqlmodel import Session, select

from marketing_intelligence.change_event_export import CSV_HEADERS, prepare_change_event_export
from marketing_intelligence.change_event_query import has_change_events, load_change_events
from marketing_intelligence.change_event_view_state import set_change_event_viewed
from marketing_intelligence.config import Settings
from marketing_intelligence.crawl_history import run_crawl
from marketing_intelligence.crawler import CrawlSettings, Crawler
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    ChangeEventViewState,
    CrawlPagePriceRecord,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    PriceChangeEvent,
    Site,
    SnapshotChangeEvent,
)
from marketing_intelligence.sites import add_site, delete_site


NOW = datetime(2026, 7, 20, 9, tzinfo=UTC)


def _seed_history(engine, *, count: int = 22, site_name: str = '=Опасный,"сайт"'):
    with Session(engine) as session:
        site = Site(name=site_name, url="https://example.test")
        other = Site(name="Другой", url="https://other.test")
        session.add(site)
        session.add(other)
        session.flush()
        runs = []
        other_runs = []
        for number in range(2):
            completed = NOW + timedelta(hours=number)
            run = CrawlRun(
                site_id=site.id,
                started_at=completed,
                completed_at=completed,
                status="completed",
                message="ok",
                max_pages=500,
                max_depth=1,
                delay=0,
                timeout=5,
                user_agent="test",
            )
            other_run = CrawlRun(
                site_id=other.id,
                started_at=completed,
                completed_at=completed,
                status="completed",
                message="ok",
                max_pages=1,
                max_depth=0,
                delay=0,
                timeout=5,
                user_agent="test",
            )
            session.add(run)
            session.add(other_run)
            session.flush()
            runs.append(run)
            other_runs.append(other_run)
        page_pairs = []
        for index in range(count):
            url = f"https://example.test/page-{index:03d}"
            pair = []
            for number, run in enumerate(runs):
                page = CrawlPageRecord(
                    crawl_run_id=run.id,
                    sequence_number=index + 1,
                    url=url,
                    depth=0,
                    outcome="html",
                    message="ok",
                    http_status=200,
                )
                session.add(page)
                session.flush()
                title = "Старая" if number == 0 else (
                    "=Новая,\nстрока" if index == 0 else f"Новая {index}"
                )
                session.add(
                    CrawlPageSnapshot(
                        crawl_page_record_id=page.id,
                        checked_at=run.completed_at,
                        title=title,
                        description=None,
                        h1=title,
                        normalized_text=title.lower(),
                        content_hash=str(number) * 64,
                        internal_links_json="[]",
                    )
                )
                pair.append(page)
            page_pairs.append(pair)
            session.add(
                SnapshotChangeEvent(
                    current_run_id=runs[1].id,
                    previous_run_id=runs[0].id,
                    current_page_record_id=pair[1].id,
                    previous_page_record_id=pair[0].id,
                    event_type="title_changed",
                    url=url,
                    current_completed_at=runs[1].completed_at,
                    importance="medium",
                    weight=2,
                )
            )
        price_url = "https://example.test/product"
        price_pages = []
        for number, run in enumerate(runs):
            page = CrawlPageRecord(
                crawl_run_id=run.id,
                sequence_number=count + 1,
                url=price_url,
                depth=0,
                outcome="html",
                message="ok",
                http_status=200,
            )
            session.add(page)
            session.flush()
            session.add(
                CrawlPageSnapshot(
                    crawl_page_record_id=page.id,
                    checked_at=run.completed_at,
                    title="Товар",
                    description=None,
                    h1="Товар",
                    normalized_text="товар",
                    content_hash=str(number) * 64,
                    internal_links_json="[]",
                )
            )
            session.add(
                CrawlPagePriceRecord(
                    crawl_page_snapshot_id=page.id,
                    sequence_number=1,
                    amount_text="100" if number == 0 else "120",
                    currency="RUB",
                    kind="price",
                    source="json-ld",
                )
            )
            price_pages.append(page)
        session.add(
            PriceChangeEvent(
                current_run_id=runs[1].id,
                previous_run_id=runs[0].id,
                current_page_record_id=price_pages[1].id,
                previous_page_record_id=price_pages[0].id,
                url=price_url,
                current_completed_at=runs[1].completed_at,
                profile="price",
                currency="RUB",
            )
        )
        other_pages = []
        for number, run in enumerate(other_runs):
            page = CrawlPageRecord(
                crawl_run_id=run.id,
                sequence_number=1,
                url="https://other.test/page",
                depth=0,
                outcome="html",
                message="ok",
                http_status=200,
            )
            session.add(page)
            session.flush()
            session.add(
                CrawlPageSnapshot(
                    crawl_page_record_id=page.id,
                    checked_at=run.completed_at,
                    title=str(number),
                    description=None,
                    h1=str(number),
                    normalized_text=str(number),
                    content_hash=str(number) * 64,
                    internal_links_json="[]",
                )
            )
            other_pages.append(page)
        other_event = SnapshotChangeEvent(
            current_run_id=other_runs[1].id,
            previous_run_id=other_runs[0].id,
            current_page_record_id=other_pages[1].id,
            previous_page_record_id=other_pages[0].id,
            event_type="title_changed",
            url="https://other.test/page",
            current_completed_at=other_runs[1].completed_at,
            importance="low",
            weight=1,
        )
        session.add(other_event)
        session.commit()
        snapshot_id = session.exec(
            select(SnapshotChangeEvent.id)
            .where(SnapshotChangeEvent.current_run_id == runs[1].id)
            .order_by(SnapshotChangeEvent.id)
        ).first()
        price_id = session.exec(
            select(PriceChangeEvent.id).where(PriceChangeEvent.current_run_id == runs[1].id)
        ).one()
        return site.id, other.id, snapshot_id, price_id, other_event.id


def _app_for(path: Path):
    return create_app(
        Settings(path.parent / "data", path.parent / "logs", f"sqlite:///{path.as_posix()}")
    )


def test_old_sqlite_adds_only_view_state_table_and_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    ChangeEventViewState.__table__.drop(engine)
    with Session(engine) as session:
        session.add(Site(name="Старый сайт", url="https://old.test"))
        session.commit()
    before_tables = set(inspect(engine).get_table_names())
    initialize_database(engine)
    after_tables = set(inspect(engine).get_table_names())
    assert after_tables - before_tables == {"changeeventviewstate"}
    with Session(engine) as session:
        assert session.exec(select(Site.name)).one() == "Старый сайт"
    engine.dispose()


def test_view_state_idempotency_filter_two_queries_delete_and_site_isolation(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    initialize_database(engine)
    site_id, other_id, snapshot_id, price_id, other_event_id = _seed_history(engine, count=2)
    first = set_change_event_viewed(
        engine,
        site_id=site_id,
        source="snapshot",
        event_id=snapshot_id,
        viewed=True,
        now=NOW,
    )
    second = set_change_event_viewed(
        engine,
        site_id=site_id,
        source="snapshot",
        event_id=snapshot_id,
        viewed=True,
        now=NOW + timedelta(days=1),
    )
    assert first.changed is True and second.changed is False
    assert first.viewed_at == second.viewed_at == NOW
    assert set_change_event_viewed(
        engine, site_id=other_id, source="snapshot", event_id=snapshot_id, viewed=True
    ).found is False
    set_change_event_viewed(
        engine, site_id=site_id, source="price", event_id=price_id, viewed=True
    )
    set_change_event_viewed(
        engine, site_id=other_id, source="snapshot", event_id=other_event_id, viewed=True
    )
    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        viewed = load_change_events(engine, site_id=site_id, viewed=True)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert statements == 2
    assert viewed.total_count == 2
    assert {item.source for item in viewed.items} == {"snapshot", "price"}
    statements = 0
    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        assert has_change_events(engine, site_id=site_id) is True
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert statements == 1
    assert delete_site(engine, site_id) is True
    with Session(engine) as session:
        states = session.exec(select(ChangeEventViewState)).all()
    assert len(states) == 1 and states[0].snapshot_change_event_id == other_event_id
    assert load_change_events(engine, site_id=other_id).total_count == 1
    engine.dispose()


def test_post_is_token_bound_reversible_and_opening_detail_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "post.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    site_id, _, snapshot_id, _, _ = _seed_history(engine, count=1)
    engine.dispose()
    with TestClient(_app_for(path)) as client:
        listing = client.get(
            f"/sites/{site_id}/changes?event_type=title_changed&view_status=unviewed&page=1"
        )
        token = re.search(r'name="action_token" value="([^"]+)"', listing.text)
        assert token is not None
        detail = client.get(
            f"/sites/{site_id}/changes/{snapshot_id}?event_type=title_changed&view_status=unviewed"
        )
        assert "Статус: Не просмотрено" in detail.text
        payload = {
            "source": "snapshot",
            "action": "view",
            "action_token": token.group(1),
            "return_area": "list",
            "event_type": "title_changed",
            "view_status": "unviewed",
            "page": "1",
        }
        forbidden = client.post(
            f"/sites/{site_id}/changes/{snapshot_id}/view-state",
            data={**payload, "action_token": "bad"},
        )
        assert forbidden.status_code == 403
        wrong_action = client.post(
            f"/sites/{site_id}/changes/{snapshot_id}/view-state",
            data={**payload, "action": "unview"},
        )
        wrong_source = client.post(
            f"/sites/{site_id}/changes/{snapshot_id}/view-state",
            data={**payload, "source": "price"},
        )
        wrong_event = client.post(
            f"/sites/{site_id}/changes/{snapshot_id + 1000}/view-state",
            data=payload,
        )
        assert {wrong_action.status_code, wrong_source.status_code, wrong_event.status_code} == {403}
        saved = client.post(
            f"/sites/{site_id}/changes/{snapshot_id}/view-state",
            data=payload,
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert "event_type=title_changed" in saved.headers["location"]
        assert "view_status=unviewed" in saved.headers["location"]
        viewed_listing = client.get(f"/sites/{site_id}/changes?view_status=viewed")
        unview_token = re.search(r'name="action_token" value="([^"]+)"', viewed_listing.text)
        assert unview_token is not None and "Статус: Просмотрено" in viewed_listing.text
        removed = client.post(
            f"/sites/{site_id}/changes/{snapshot_id}/view-state",
            data={
                "source": "snapshot",
                "action": "unview",
                "action_token": unview_token.group(1),
                "return_area": "detail",
                "page": "1",
            },
            follow_redirects=False,
        )
        assert removed.status_code == 303
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    with Session(engine) as session:
        assert session.exec(select(ChangeEventViewState)).all() == []
    engine.dispose()


def test_json_csv_export_full_selection_schema_formula_safety_and_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "export.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    site_id, _, snapshot_id, price_id, _ = _seed_history(engine)
    set_change_event_viewed(
        engine, site_id=site_id, source="snapshot", event_id=snapshot_id, viewed=True, now=NOW
    )
    expected_order = [
        (item.source, item.event_id)
        for item in load_change_events(engine, site_id=site_id, limit=200).items
    ]
    engine.dispose()
    monkeypatch.setattr("marketing_intelligence.change_event_export.EXPORT_BATCH_SIZE", 2)
    before = sha256(path.read_bytes()).hexdigest()
    with TestClient(_app_for(path)) as client:
        listing = client.get(f"/sites/{site_id}/changes?page=1")
        assert "Страница 1 из 2" in listing.text
        json_response = client.get(f"/sites/{site_id}/changes/export.json?page=2")
        csv_response = client.get(f"/sites/{site_id}/changes/export.csv?page=2")
        global_json = client.get("/changes/export.json")
        viewed_json = client.get(
            f"/sites/{site_id}/changes/export.json?view_status=viewed"
        )
    assert json_response.status_code == csv_response.status_code == global_json.status_code == 200
    payload = json_response.json()
    assert payload["schema_version"] == 1
    assert len(payload["events"]) == 23
    assert {item["source"] for item in payload["events"]} == {"snapshot", "price"}
    assert [(item["source"], item["event_id"]) for item in payload["events"]] == expected_order
    assert len(viewed_json.json()["events"]) == 1
    assert any(
        item["source"] == "price"
        and item["event_id"] == price_id
        and item["current"]["low"] == "120"
        for item in payload["events"]
    )
    formula_event = next(
        item
        for item in payload["events"]
        if item["event_id"] == snapshot_id and item["source"] == "snapshot"
    )
    assert formula_event["site_name"] == '=Опасный,"сайт"'
    assert formula_event["current"] == {"state": "text", "value": "=Новая,\nстрока"}
    assert formula_event["viewed"] is True and formula_event["viewed_at"].endswith("Z")
    raw_csv = csv_response.content
    assert raw_csv.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in raw_csv.replace(b"\r\n", b"")
    rows = list(csv.reader(StringIO(raw_csv.decode("utf-8-sig"), newline="")))
    assert tuple(rows[0]) == CSV_HEADERS
    assert len(rows) == 24
    site_name_index = CSV_HEADERS.index("Сайт")
    current_index = CSV_HEADERS.index("Стало")
    assert any(row[site_name_index] == "'=Опасный,\"сайт\"" for row in rows[1:])
    assert any(row[current_index] == "'=Новая,\r\nстрока" for row in rows[1:])
    assert len(global_json.json()["events"]) == 24
    assert sha256(path.read_bytes()).hexdigest() == before


def test_export_uses_constant_queries_per_bounded_batch(tmp_path: Path, monkeypatch) -> None:
    engine = build_engine(f"sqlite:///{(tmp_path / 'batches.db').as_posix()}")
    initialize_database(engine)
    site_id, _, _, _, _ = _seed_history(engine, count=4)
    monkeypatch.setattr("marketing_intelligence.change_event_export.EXPORT_BATCH_SIZE", 2)
    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        prepared = prepare_change_event_export(
            engine,
            format_name="json",
            site_id=site_id,
            event_types=None,
            from_time=None,
            before_time=None,
            viewed=None,
            local_timezone=UTC,
        )
        content = b"".join(prepared.chunks())
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert len(json.loads(content)["events"]) == 5
    assert statements == 18  # Три пакета, по шесть запросов без запроса на событие.
    engine.dispose()


def test_empty_export_and_corrupt_data_return_safe_response(tmp_path: Path) -> None:
    path = tmp_path / "errors.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    site_id, other_id, snapshot_id, _, _ = _seed_history(engine, count=1)
    engine.dispose()
    with TestClient(_app_for(path)) as client:
        empty_json = client.get(f"/sites/{other_id}/changes/export.json?event_type=price_changed")
        empty_csv = client.get(f"/sites/{other_id}/changes/export.csv?event_type=price_changed")
        missing = client.get("/changes/export.json?site_id=999999")
        invalid = client.get("/changes/export.json?view_status=unknown")
    assert empty_json.json() == {"schema_version": 1, "events": []}
    assert empty_csv.content.startswith(b"\xef\xbb\xbf")
    assert len(list(csv.reader(StringIO(empty_csv.content.decode("utf-8-sig"))))) == 1
    assert missing.status_code == 404 and invalid.status_code == 422
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    with Session(engine) as session:
        event_row = session.get(SnapshotChangeEvent, snapshot_id)
        session.delete(session.get(CrawlPageSnapshot, event_row.current_page_record_id))
        session.commit()
    engine.dispose()
    with TestClient(_app_for(path)) as client:
        corrupt = client.get(f"/sites/{site_id}/changes/export.json")
    assert corrupt.status_code == 500
    assert corrupt.headers["content-type"].startswith("text/html")
    assert "Файл не создан" in corrupt.text


def test_filtered_empty_states_distinguish_absent_history_from_no_matches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty-states.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    empty_site = add_site(engine, "Без истории", "https://empty.test")
    engine.dispose()
    with TestClient(_app_for(path)) as client:
        local_without_history = client.get(
            f"/sites/{empty_site.id}/changes?view_status=viewed"
        )
        global_without_history = client.get(
            f"/changes?site_id={empty_site.id}&view_status=viewed"
        )
    assert "Истории ещё нет" in local_without_history.text
    assert "По фильтрам ничего не найдено" not in local_without_history.text
    assert "Общей истории ещё нет" in global_without_history.text
    assert "По фильтрам ничего не найдено" not in global_without_history.text

    engine = build_engine(f"sqlite:///{path.as_posix()}")
    history_site, _, snapshot_id, _, _ = _seed_history(
        engine,
        count=1,
        site_name="С историей",
    )
    engine.dispose()
    with TestClient(_app_for(path)) as client:
        local_no_matches = client.get(
            f"/sites/{history_site}/changes?view_status=viewed"
        )
        global_no_matches = client.get(f"/changes?site_id={empty_site.id}")
        mismatched_scope = client.get(
            f"/sites/{history_site}/changes/{snapshot_id}"
            f"?scope=competitor&site_id={empty_site.id}"
        )
    assert "По фильтрам ничего не найдено" in local_no_matches.text
    assert "Истории ещё нет" not in local_no_matches.text
    assert "По фильтрам ничего не найдено" in global_no_matches.text
    assert "Общей истории ещё нет" not in global_no_matches.text
    assert mismatched_scope.status_code == 422
    assert "Фильтр сайта не соответствует открытому событию" in mismatched_scope.text


def test_actual_loopback_view_filter_export_restart_and_read_only_sha(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    version = {"value": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
                content_type = "text/plain"
            else:
                price = "100.00" if version["value"] == 1 else "120.00"
                title = "Было" if version["value"] == 1 else "Стало"
                body = (
                    "<html><head><title>"
                    + title
                    + '</title><script type="application/ld+json">'
                    + '{"@type":"Offer","price":"'
                    + price
                    + '","priceCurrency":"RUB"}'
                    + "</script></head><body><h1>Товар</h1></body></html>"
                ).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "loopback.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    site = add_site(engine, "Loopback", f"http://127.0.0.1:{server.server_port}/")

    async def no_wait(_seconds: float) -> None:
        return None

    async def crawl_twice() -> None:
        crawler = Crawler(delay=no_wait)
        settings = CrawlSettings(
            max_pages=1,
            max_depth=0,
            delay=0.5,
            timeout=3,
            user_agent="Task0031Loopback/1.0",
        )
        await run_crawl(engine, site.id, site.url, crawler=crawler, settings=settings)
        version["value"] = 2
        await run_crawl(engine, site.id, site.url, crawler=crawler, settings=settings)

    try:
        asyncio.run(crawl_twice())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        engine.dispose()

    settings = Settings(
        tmp_path / "loopback-data",
        tmp_path / "loopback-logs",
        f"sqlite:///{path.as_posix()}",
    )
    with TestClient(create_app(settings)) as client:
        listing = client.get(f"/sites/{site.id}/changes")
        forms = re.findall(
            r'<form method="post" action="([^"]+/view-state)">(.*?)</form>',
            listing.text,
            re.DOTALL,
        )
        sources = set()
        for action_url, form in forms:
            source = re.search(r'name="source" value="([^"]+)"', form).group(1)
            if source in sources:
                continue
            sources.add(source)
            token = re.search(r'name="action_token" value="([^"]+)"', form).group(1)
            response = client.post(
                action_url,
                data={
                    "source": source,
                    "action": "view",
                    "action_token": token,
                    "return_area": "list",
                    "page": "1",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
        assert sources == {"snapshot", "price"}
        viewed = client.get(f"/sites/{site.id}/changes?view_status=viewed")
        exported_json = client.get(
            f"/sites/{site.id}/changes/export.json?view_status=viewed"
        )
        exported_csv = client.get(
            f"/sites/{site.id}/changes/export.csv?view_status=viewed"
        )
        assert viewed.text.count("Статус: Просмотрено") >= 2
        assert {item["source"] for item in exported_json.json()["events"]} == {
            "snapshot",
            "price",
        }
        assert "120 RUB" in exported_csv.content.decode("utf-8-sig")
    before = sha256(path.read_bytes()).hexdigest()
    with TestClient(create_app(settings)) as client:
        listing = client.get(f"/sites/{site.id}/changes?view_status=viewed")
        detail_href = re.search(r'href="([^"]+/changes/\d+[^"]*)">Подробнее', listing.text)
        assert detail_href is not None
        detail = client.get(detail_href.group(1).replace("&amp;", "&"))
        json_response = client.get(f"/sites/{site.id}/changes/export.json")
        csv_response = client.get(f"/sites/{site.id}/changes/export.csv")
        assert all(
            response.status_code == 200
            for response in (listing, detail, json_response, csv_response)
        )
    assert sha256(path.read_bytes()).hexdigest() == before
