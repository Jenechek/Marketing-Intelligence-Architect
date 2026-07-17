from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session

from marketing_intelligence.change_event import ChangeEventType
from marketing_intelligence.change_event_detail import (
    ChangeEventDataError,
    load_change_event,
)
from marketing_intelligence.config import Settings
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    Site,
    SnapshotChangeEvent,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


@pytest.fixture
def seeded_database(tmp_path: Path) -> tuple[Path, int, int, dict[ChangeEventType, int]]:
    path = tmp_path / "events.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    with Session(engine) as session:
        site = Site(name="Основной <сайт>", url="https://example.test")
        other = Site(name="Чужой", url="https://other.test")
        session.add(site)
        session.add(other)
        session.flush()
        previous_run = _run(session, site.id, NOW - timedelta(days=1))
        current_run = _run(session, site.id, NOW)
        other_previous = _run(session, other.id, NOW - timedelta(days=1))
        other_current = _run(session, other.id, NOW)
        ids = _all_events(session, previous_run, current_run)
        other_event = _event(
            session,
            other_previous,
            other_current,
            ChangeEventType.PAGE_ADDED,
            "https://other.test/new",
            current_page=_page(
                session,
                other_current,
                "https://other.test/new",
                title="other",
            ),
        )
        session.commit()
        assert site.id is not None and other_event is not None
        site_id = site.id
    engine.dispose()
    return path, site_id, other_event, ids


def _run(session: Session, site_id: int | None, completed_at: datetime) -> int:
    assert site_id is not None
    run = CrawlRun(
        site_id=site_id,
        started_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
        status="completed",
        message="ok",
        max_pages=20,
        max_depth=2,
        delay=0,
        timeout=5,
        user_agent="test",
    )
    session.add(run)
    session.flush()
    assert run.id is not None
    return run.id


def _page(
    session: Session,
    run_id: int,
    url: str,
    *,
    title: str | None = "title",
    description: str | None = "description",
    h1: str | None = "h1",
    normalized_text: str = "text",
    links: object = None,
) -> int:
    page = CrawlPageRecord(
        crawl_run_id=run_id,
        sequence_number=1,
        url=url,
        depth=0,
        outcome="success",
        message="ok",
        http_status=200,
    )
    session.add(page)
    session.flush()
    assert page.id is not None
    session.add(
        CrawlPageSnapshot(
            crawl_page_record_id=page.id,
            checked_at=NOW,
            title=title,
            description=description,
            h1=h1,
            normalized_text=normalized_text,
            content_hash="hash",
            internal_links_json=json.dumps([] if links is None else links),
        )
    )
    return page.id


def _event(
    session: Session,
    previous_run: int,
    current_run: int,
    event_type: ChangeEventType,
    url: str,
    *,
    current_page: int | None,
    previous_page: int | None = None,
) -> int:
    event = SnapshotChangeEvent(
        current_run_id=current_run,
        previous_run_id=previous_run,
        current_page_record_id=current_page,
        previous_page_record_id=previous_page,
        event_type=event_type.value,
        url=url,
        current_completed_at=NOW,
        importance="high",
        weight=3,
        text_distance=1 if event_type is ChangeEventType.TEXT_CHANGED else None,
        change_ratio_numerator=(
            1
            if event_type
            in {ChangeEventType.TEXT_CHANGED, ChangeEventType.INTERNAL_LINKS_CHANGED}
            else None
        ),
        change_ratio_denominator=(
            2
            if event_type
            in {ChangeEventType.TEXT_CHANGED, ChangeEventType.INTERNAL_LINKS_CHANGED}
            else None
        ),
    )
    session.add(event)
    session.flush()
    assert event.id is not None
    return event.id


def _all_events(
    session: Session,
    previous_run: int,
    current_run: int,
) -> dict[ChangeEventType, int]:
    ids: dict[ChangeEventType, int] = {}
    for event_type in ChangeEventType:
        url = f"https://example.test/{event_type.value}?x=<tag>"
        if event_type is ChangeEventType.PAGE_ADDED:
            current_page = _page(session, current_run, url, title="новая")
            previous_page = None
        elif event_type is ChangeEventType.PAGE_REMOVED:
            current_page = None
            previous_page = _page(session, previous_run, url, title="удалённая")
        else:
            current_kwargs: dict[str, object] = {}
            previous_kwargs: dict[str, object] = {}
            if event_type is ChangeEventType.TITLE_CHANGED:
                current_kwargs["title"] = ""
                previous_kwargs["title"] = None
            elif event_type is ChangeEventType.DESCRIPTION_CHANGED:
                current_kwargs["description"] = "<script>alert('x')</script>"
                previous_kwargs["description"] = "описание & старое"
            elif event_type is ChangeEventType.H1_CHANGED:
                current_kwargs["h1"] = "Новый " + "длинный" * 100
                previous_kwargs["h1"] = "Старый"
            elif event_type is ChangeEventType.TEXT_CHANGED:
                current_kwargs["normalized_text"] = "новый\nточный текст"
                previous_kwargs["normalized_text"] = "старый точный текст"
            else:
                current_kwargs["links"] = ["/б", "/a", "/a"]
                previous_kwargs["links"] = ["/c", "/б", "/c"]
            current_page = _page(session, current_run, url, **current_kwargs)
            previous_page = _page(session, previous_run, url, **previous_kwargs)
        ids[event_type] = _event(
            session,
            previous_run,
            current_run,
            event_type,
            url,
            current_page=current_page,
            previous_page=previous_page,
        )
    return ids


def _engine(path: Path):
    return build_engine(f"sqlite:///{path.as_posix()}")


def test_loads_all_seven_types_with_current_first_and_exact_values(
    seeded_database,
) -> None:
    path, site_id, _, ids = seeded_database
    engine = _engine(path)

    details = {
        event_type: load_change_event(engine, site_id=site_id, event_id=event_id)
        for event_type, event_id in ids.items()
    }
    engine.dispose()

    assert set(details) == set(ChangeEventType)
    assert details[ChangeEventType.PAGE_ADDED].current is not None
    assert details[ChangeEventType.PAGE_ADDED].previous is None
    assert details[ChangeEventType.PAGE_REMOVED].current is None
    assert details[ChangeEventType.PAGE_REMOVED].previous is not None
    title = details[ChangeEventType.TITLE_CHANGED]
    assert title.current.title == ""
    assert title.previous.title is None
    assert details[ChangeEventType.DESCRIPTION_CHANGED].current.description == (
        "<script>alert('x')</script>"
    )
    assert details[ChangeEventType.TEXT_CHANGED].current.normalized_text == (
        "новый\nточный текст"
    )
    links = details[ChangeEventType.INTERNAL_LINKS_CHANGED]
    assert links.current.internal_links == ("/a", "/б")
    assert links.previous.internal_links == ("/c", "/б")
    assert links.change_ratio == Fraction(1, 2)
    with pytest.raises(FrozenInstanceError):
        title.current = None


def test_site_isolation_and_unknown_event_are_indistinguishable(
    seeded_database,
) -> None:
    path, site_id, other_event, _ = seeded_database
    engine = _engine(path)
    assert load_change_event(engine, site_id=site_id, event_id=other_event) is None
    assert load_change_event(engine, site_id=site_id, event_id=999_999) is None
    engine.dispose()


def test_invalid_json_and_broken_page_links_fail_clearly(seeded_database) -> None:
    path, site_id, _, ids = seeded_database
    engine = _engine(path)
    detail = load_change_event(
        engine,
        site_id=site_id,
        event_id=ids[ChangeEventType.INTERNAL_LINKS_CHANGED],
    )
    assert detail is not None
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE crawlpagesnapshot SET internal_links_json = '{' "
                "WHERE crawl_page_record_id = :page_id"
            ),
            {
                "page_id": _current_page_id(
                    connection,
                    ids[ChangeEventType.INTERNAL_LINKS_CHANGED],
                )
            },
        )
    with pytest.raises(ChangeEventDataError, match="повреждённый JSON"):
        load_change_event(
            engine,
            site_id=site_id,
            event_id=ids[ChangeEventType.INTERNAL_LINKS_CHANGED],
        )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE snapshotchangeevent SET url = 'https://wrong.test' WHERE id = :id"),
            {"id": ids[ChangeEventType.TITLE_CHANGED]},
        )
    with pytest.raises(ChangeEventDataError, match="URL события"):
        load_change_event(
            engine,
            site_id=site_id,
            event_id=ids[ChangeEventType.TITLE_CHANGED],
        )
    engine.dispose()


@pytest.mark.parametrize(
    ("statement", "event_type", "message"),
    [
        (
            "UPDATE crawlrun SET site_id = 2 WHERE id = "
            "(SELECT previous_run_id FROM snapshotchangeevent WHERE id = :id)",
            ChangeEventType.TITLE_CHANGED,
            "ссылки на завершённые обходы",
        ),
        (
            "UPDATE crawlpagerecord SET crawl_run_id = "
            "(SELECT previous_run_id FROM snapshotchangeevent WHERE id = :id) "
            "WHERE id = (SELECT current_page_record_id FROM snapshotchangeevent WHERE id = :id)",
            ChangeEventType.H1_CHANGED,
            "обходу или URL",
        ),
        (
            "DELETE FROM crawlpagesnapshot WHERE crawl_page_record_id = "
            "(SELECT current_page_record_id FROM snapshotchangeevent WHERE id = :id)",
            ChangeEventType.TEXT_CHANGED,
            "не найден сохранённый снимок",
        ),
    ],
)
def test_corrupt_run_page_and_snapshot_references_are_controlled(
    seeded_database,
    statement: str,
    event_type: ChangeEventType,
    message: str,
) -> None:
    path, site_id, _, ids = seeded_database
    engine = _engine(path)
    with engine.begin() as connection:
        connection.execute(text(statement), {"id": ids[event_type]})
    with pytest.raises(ChangeEventDataError, match=message):
        load_change_event(engine, site_id=site_id, event_id=ids[event_type])
    engine.dispose()


def _current_page_id(connection, event_id: int) -> int:
    return connection.execute(
        text("SELECT current_page_record_id FROM snapshotchangeevent WHERE id = :id"),
        {"id": event_id},
    ).scalar_one()


def test_reopened_sqlite_read_is_byte_for_byte_read_only(seeded_database) -> None:
    path, site_id, _, ids = seeded_database
    before = sha256(path.read_bytes()).hexdigest()
    engine = _engine(path)
    detail = load_change_event(
        engine,
        site_id=site_id,
        event_id=ids[ChangeEventType.TEXT_CHANGED],
    )
    engine.dispose()
    assert detail is not None
    assert detail.current.normalized_text == "новый\nточный текст"
    assert sha256(path.read_bytes()).hexdigest() == before


def test_server_rendered_list_detail_order_escaping_focus_and_errors(
    seeded_database,
    tmp_path: Path,
) -> None:
    path, site_id, other_event, ids = seeded_database
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        home = client.get("/")
        listing = client.get(f"/sites/{site_id}/changes")
        detail = client.get(
            f"/sites/{site_id}/changes/{ids[ChangeEventType.DESCRIPTION_CHANGED]}"
        )
        missing = client.get(f"/sites/{site_id}/changes/{other_event}")

    assert f'href="/sites/{site_id}/changes">Изменения</a>' in home.text
    assert listing.status_code == 200
    assert "Новая страница" in listing.text
    assert "Изменение внутренних ссылок" in listing.text
    assert "Высокая" in listing.text
    assert detail.status_code == 200
    assert detail.text.index(">Стало<") < detail.text.index(">Было<")
    assert "Текущее значение" in detail.text and "Предыдущее значение" in detail.text
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in detail.text
    assert "<script>alert('x')</script>" not in detail.text
    assert missing.status_code == 404
    assert "В этом сайте такого события нет" in missing.text
    styles = (
        Path(__file__).parents[1]
        / "src/marketing_intelligence/static/styles.css"
    ).read_text(encoding="utf-8")
    assert "focus-visible" in styles


def test_empty_event_list_has_clear_state(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    engine = _engine(path)
    initialize_database(engine)
    with Session(engine) as session:
        site = Site(name="Пустой", url="https://empty.test")
        session.add(site)
        session.commit()
        session.refresh(site)
        site_id = site.id
    engine.dispose()
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        response = client.get(f"/sites/{site_id}/changes")
    assert response.status_code == 200
    assert "Изменений пока нет" in response.text


def test_real_loopback_two_completed_crawls_open_exact_values_after_reopen(
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
                self.send_response(404)
                self.end_headers()
                return
            if version["value"] == 1:
                body = (
                    '<html><head><title>Было &amp; точно</title>'
                    '<meta name="description" content="Старое описание"></head>'
                    '<body><h1>Старый H1</h1><p>Старый текст</p>'
                    '<a href="/old">old</a></body></html>'
                ).encode()
            else:
                body = (
                    '<html><head><title>Стало &lt;точно&gt;</title>'
                    '<meta name="description" content="Новое описание"></head>'
                    '<body><h1>Новый H1</h1><p>Новый текст</p>'
                    '<a href="/new">new</a></body></html>'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database_path = tmp_path / "loopback-data" / "test.db"
    settings = Settings(
        data_dir=tmp_path / "loopback-data",
        logs_dir=tmp_path / "loopback-logs",
        database_url=f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(settings)

    def run_once(client: TestClient) -> None:
        token_response = client.get("/sites/1/crawl")
        token = re.search(
            r'name="action_token" value="([^"]+)"',
            token_response.text,
        )
        assert token is not None
        response = client.post(
            "/sites/1/crawl",
            data={
                "action_token": token.group(1),
                "max_pages": "1",
                "max_depth": "0",
                "delay": "0,5",
                "timeout": "3",
                "user_agent": "Task0027Loopback/1.0",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        for _ in range(300):
            status = client.get(response.headers["location"])
            if 'data-run-status="completed"' in status.text:
                return
            time.sleep(0.01)
        pytest.fail("Loopback-обход не завершился вовремя")

    try:
        with TestClient(app) as client:
            create = client.post(
                "/sites",
                data={
                    "name": "Loopback",
                    "url": f"http://127.0.0.1:{server.server_port}/",
                },
                follow_redirects=False,
            )
            assert create.status_code == 303
            run_once(client)
            version["value"] = 2
            run_once(client)
            listing = client.get("/sites/1/changes")
            assert "Изменение Title" in listing.text
            href = re.search(
                r'href="(/sites/1/changes/\d+)">Подробнее</a>',
                listing.text,
            )
            assert href is not None
            title_href = re.search(
                r"Изменение Title.*?href=\"(/sites/1/changes/\d+)\"",
                listing.text,
                re.DOTALL,
            )
            assert title_href is not None
            detail = client.get(title_href.group(1))
            assert detail.text.index(">Стало<") < detail.text.index(">Было<")
            assert "Стало &lt;точно&gt;" in detail.text
            assert "Было &amp; точно" in detail.text
            detail_path = title_href.group(1)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    reopened = create_app(settings)
    with TestClient(reopened) as client:
        listing = client.get("/sites/1/changes")
        detail = client.get(detail_path)
        assert listing.status_code == 200
        assert detail.status_code == 200
        assert "Стало &lt;точно&gt;" in detail.text
        assert "Было &amp; точно" in detail.text
