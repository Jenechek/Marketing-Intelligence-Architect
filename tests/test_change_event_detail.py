from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from html import unescape
from pathlib import Path
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event, text
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
    completed_at: datetime = NOW,
) -> int:
    event = SnapshotChangeEvent(
        current_run_id=current_run,
        previous_run_id=previous_run,
        current_page_record_id=current_page,
        previous_page_record_id=previous_page,
        event_type=event_type.value,
        url=url,
        current_completed_at=completed_at,
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


def test_global_feed_filters_explanations_pagination_return_and_two_queries(
    seeded_database,
    tmp_path: Path,
) -> None:
    path, site_id, _, ids = seeded_database
    engine = _engine(path)
    with Session(engine) as session:
        source = session.get(
            SnapshotChangeEvent,
            ids[ChangeEventType.PAGE_ADDED],
        )
        assert source is not None
        for number in range(20):
            url = f"https://example.test/global-{number}?unsafe=<tag>"
            page_id = _page(session, source.current_run_id, url)
            _event(
                session,
                source.previous_run_id,
                source.current_run_id,
                ChangeEventType.PAGE_ADDED,
                url,
                current_page=page_id,
            )
        session.commit()
    engine.dispose()
    before = sha256(path.read_bytes()).hexdigest()
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{path.as_posix()}",
            local_timezone=timezone(timedelta(hours=3)),
        )
    )
    event_sql: list[str] = []
    with TestClient(app) as client:
        home = client.get("/")
        first = client.get("/changes")

        def record_sql(connection, cursor, statement, parameters, context, executemany):
            if "snapshotchangeevent" in statement.lower():
                event_sql.append(statement)

        sqlalchemy_event.listen(app.state.engine, "before_cursor_execute", record_sql)
        try:
            second = client.get("/changes?page=2")
        finally:
            sqlalchemy_event.remove(app.state.engine, "before_cursor_execute", record_sql)

        detail_match = re.search(
            r'href="(/sites/\d+/changes/\d+\?scope=competitor&amp;page=2)"',
            second.text,
        )
        assert detail_match is not None
        detail = client.get(unescape(detail_match.group(1)))
        filtered = client.get(
            f"/changes?site_id={site_id}&event_type=title_changed"
            "&date_from=2026-07-17&date_to=2026-07-17"
        )
        filtered_match = re.search(
            r'href="([^"]+scope=competitor[^"]+)"', filtered.text
        )
        assert filtered_match is not None
        filtered_detail = client.get(unescape(filtered_match.group(1)))
        bad_site = client.get("/changes?site_id=%3Cscript%3E")
        missing_site = client.get("/changes?site_id=999999")
        bad_type = client.get("/changes?event_type=unknown")
        bad_date = client.get("/changes?date_from=17.07.2026")
        bad_page = client.get("/changes?page=0")
        missing_page = client.get(
            f"/changes?site_id={site_id}&event_type=title_changed&page=2"
        )
        no_matches = client.get("/changes?date_from=2030-01-01")
        unsafe_return = client.get(
            unescape(filtered_match.group(1))
            + "&return_url=https://attacker.example/"
        )
        bad_scope = client.get(
            f"/sites/{site_id}/changes/{ids[ChangeEventType.TITLE_CHANGED]}"
            "?scope=https://attacker.example/"
        )

    combined = first.text + second.text
    assert 'href="/competitors/changes">История изменений конкурентов</a>' in home.text
    assert first.status_code == second.status_code == detail.status_code == 200
    assert "Найдено: 28." in first.text
    assert "Основной &lt;сайт&gt;" in combined and "Чужой" in combined
    assert "https://other.test/new" in combined
    assert "&lt;tag&gt;" in combined and "<tag>" not in combined
    for explanation in (
        "найдена страница, которой не было",
        "была в предыдущем завершённом обходе",
        "Значение Title страницы отличается",
        "Значение Description страницы отличается",
        "Значение H1 страницы отличается",
        "Нормализованный текст страницы отличается",
        "Набор внутренних ссылок страницы отличается",
    ):
        assert explanation in combined
    assert len(event_sql) == 2
    assert sum("count(" in statement.lower() for statement in event_sql) == 1
    assert 'href="/competitors/changes?page=2">К событиям</a>' in detail.text
    assert filtered.status_code == filtered_detail.status_code == 200
    assert "Найдено: 1." in filtered.text
    assert "2026-07-17 15:00:00+03:00" in filtered.text
    assert (
        f'href="/competitors/changes?site_id={site_id}&amp;event_type=title_changed'
        '&amp;date_from=2026-07-17&amp;date_to=2026-07-17">К событиям</a>'
    ) in filtered_detail.text
    assert bad_site.status_code == bad_type.status_code == bad_date.status_code == 422
    assert "&lt;script&gt;" in bad_site.text and "<script>" not in bad_site.text
    assert missing_site.status_code == 404
    assert "Выбранный сайт не существует" in missing_site.text
    assert bad_page.status_code == 422
    assert missing_page.status_code == 404
    assert f'href="/competitors/changes?site_id={site_id}&amp;event_type=title_changed"' in missing_page.text
    assert "По фильтрам ничего не найдено" in no_matches.text
    assert "attacker.example" not in unsafe_return.text
    assert bad_scope.status_code == 422
    assert sha256(path.read_bytes()).hexdigest() == before


def test_global_feed_distinguishes_no_sites_empty_history_and_no_matches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "global-empty.db"
    engine = _engine(path)
    initialize_database(engine)
    engine.dispose()
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        no_sites = client.get("/changes")
        invalid_without_sites = client.get(
            "/changes?event_type=%3Cscript%3Ealert(1)%3C%2Fscript%3E"
        )
        client.post(
            "/sites",
            data={"name": "Пустой", "url": "https://empty.test"},
        )
        no_history = client.get("/changes")
    assert "Сайтов пока нет" in no_sites.text
    assert invalid_without_sites.status_code == 422
    assert "Сайтов пока нет" in invalid_without_sites.text
    assert (
        "Выберите один из доступных типов события"
        in invalid_without_sites.text
    )
    assert "<script>alert(1)</script>" not in invalid_without_sites.text
    assert "Общей истории ещё нет" in no_history.text


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
    assert "Истории ещё нет" in response.text


def test_filters_dates_pagination_links_errors_and_read_only(
    seeded_database,
    tmp_path: Path,
) -> None:
    path, site_id, _, ids = seeded_database
    engine = _engine(path)
    with Session(engine) as session:
        source_event = session.get(
            SnapshotChangeEvent,
            ids[ChangeEventType.TITLE_CHANGED],
        )
        assert source_event is not None
        current_run = source_event.current_run_id
        previous_run = source_event.previous_run_id
        for number in range(18):
            url = f"https://example.test/more-{number}?value=<unsafe>"
            page_id = _page(session, current_run, url, title=f"more {number}")
            _event(
                session,
                previous_run,
                current_run,
                ChangeEventType.PAGE_ADDED,
                url,
                current_page=page_id,
            )
        session.commit()
    engine.dispose()
    before = sha256(path.read_bytes()).hexdigest()
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{path.as_posix()}",
            local_timezone=timezone(timedelta(hours=3)),
        )
    )
    event_sql: list[str] = []
    with TestClient(app) as client:
        by_type = client.get(
            f"/sites/{site_id}/changes?event_type=title_changed"
        )
        by_from = client.get(f"/sites/{site_id}/changes?date_from=2026-07-17")
        by_to = client.get(f"/sites/{site_id}/changes?date_to=2026-07-17")
        combined = client.get(
            f"/sites/{site_id}/changes?event_type=title_changed"
            "&date_from=2026-07-17&date_to=2026-07-17"
        )
        first = client.get(
            f"/sites/{site_id}/changes?date_from=2026-07-17&date_to=2026-07-17"
        )

        def record_sql(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            if "snapshotchangeevent" in statement.lower():
                event_sql.append(statement)

        sqlalchemy_event.listen(
            app.state.engine,
            "before_cursor_execute",
            record_sql,
        )
        try:
            second = client.get(
                f"/sites/{site_id}/changes?date_from=2026-07-17"
                "&date_to=2026-07-17&page=2"
            )
        finally:
            sqlalchemy_event.remove(
                app.state.engine,
                "before_cursor_execute",
                record_sql,
            )

        first_ids = set(re.findall(r"/changes/(\d+)\?", first.text))
        second_ids = set(re.findall(r"/changes/(\d+)\?", second.text))
        detail_href_match = re.search(
            r'href="([^"]*/changes/\d+\?[^\"]+)"', second.text
        )
        assert detail_href_match is not None
        detail_href = unescape(detail_href_match.group(1))
        detail = client.get(detail_href)

    assert by_type.status_code == 200
    assert "Найдено: 1." in by_type.text
    assert "Изменение Title" in by_type.text
    assert "Найдено: 25." in by_from.text
    assert "Найдено: 25." in by_to.text
    assert "Найдено: 1." in combined.text
    assert "2026-07-17 15:00:00+03:00" in by_type.text
    assert "2026-07-17 15:00:00+03:00" in combined.text
    assert first.status_code == second.status_code == detail.status_code == 200
    assert "2026-07-17 15:00:00+03:00" in detail.text
    assert len(event_sql) == 2
    assert sum("count(" in statement.lower() for statement in event_sql) == 1
    assert "Страница 1 из 2" in first.text
    assert "Страница 2 из 2" in second.text
    assert len(first_ids) == 20 and len(second_ids) == 5
    assert first_ids.isdisjoint(second_ids)
    assert "date_from=2026-07-17&amp;date_to=2026-07-17&amp;page=2" in first.text
    assert "date_from=2026-07-17&amp;date_to=2026-07-17" in second.text
    assert f'href="/sites/{site_id}/changes?date_from=2026-07-17&amp;date_to=2026-07-17&amp;page=2"' in detail.text
    assert f'action="/sites/{site_id}/changes"' in second.text
    assert f'href="/sites/{site_id}/changes">Сбросить</a>' in second.text
    assert "https://other.test/new" not in first.text + second.text
    assert "&lt;unsafe&gt;" in first.text + second.text
    assert "<unsafe>" not in first.text + second.text
    assert sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("event_type=unknown", "Выберите один из доступных типов события"),
        ("date_from=17.07.2026", "Дата «с» указана неверно"),
        ("date_to=tomorrow", "Дата «по» указана неверно"),
        ("date_to=9999-12-31", "Дата выходит за поддерживаемый диапазон"),
        (
            "date_from=2026-07-18&date_to=2026-07-17",
            "Дата «с» не может быть позже даты «по»",
        ),
        ("page=no", "Номер страницы должен быть положительным целым числом"),
        ("page=0", "Номер страницы должен быть положительным целым числом"),
        ("page=-1", "Номер страницы должен быть положительным целым числом"),
    ],
)
def test_invalid_list_parameters_are_russian_html_422_and_preserved(
    seeded_database,
    tmp_path: Path,
    query: str,
    message: str,
) -> None:
    path, site_id, _, _ = seeded_database
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        response = client.get(f"/sites/{site_id}/changes?{query}")
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert message in response.text
    for value in (item.split("=", 1)[1] for item in query.split("&")):
        assert value in response.text


def test_invalid_values_are_escaped_and_missing_page_has_filtered_first_link(
    seeded_database,
    tmp_path: Path,
) -> None:
    path, site_id, _, _ = seeded_database
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        escaped = client.get(
            f"/sites/{site_id}/changes?event_type=%3Cscript%3Ealert(1)%3C/script%3E"
        )
        missing = client.get(
            f"/sites/{site_id}/changes?event_type=title_changed&page=999999999999999999999"
        )
        no_matches = client.get(
            f"/sites/{site_id}/changes?date_from=2030-01-01"
        )
    assert escaped.status_code == 422
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in escaped.text
    assert "<script>alert(1)</script>" not in escaped.text
    assert missing.status_code == 404
    assert "Страница событий не найдена" in missing.text
    assert f'href="/sites/{site_id}/changes?event_type=title_changed"' in missing.text
    assert no_matches.status_code == 200
    assert "По фильтрам ничего не найдено" in no_matches.text


def test_real_loopback_two_sites_two_crawls_global_filter_detail_and_return(
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

    def run_once(client: TestClient, site_id: int) -> None:
        token_response = client.get(f"/sites/{site_id}/crawl")
        token = re.search(
            r'name="action_token" value="([^"]+)"',
            token_response.text,
        )
        assert token is not None
        response = client.post(
            f"/sites/{site_id}/crawl",
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
            create_second = client.post(
                "/sites",
                data={
                    "name": "Loopback второй",
                    "url": f"http://127.0.0.1:{server.server_port}/",
                },
                follow_redirects=False,
            )
            assert create_second.status_code == 303
            run_once(client, 1)
            run_once(client, 2)
            version["value"] = 2
            run_once(client, 1)
            run_once(client, 2)
            global_listing = client.get("/changes")
            assert "Loopback" in global_listing.text
            assert "Loopback второй" in global_listing.text
            global_filtered = client.get(
                "/changes?site_id=1&event_type=title_changed"
            )
            global_href = re.search(
                r'href="(/sites/1/changes/\d+\?scope=competitor&amp;site_id=1&amp;event_type=title_changed)"',
                global_filtered.text,
            )
            assert global_href is not None
            global_detail_path = unescape(global_href.group(1))
            global_detail = client.get(global_detail_path)
            assert (
                'href="/competitors/changes?site_id=1&amp;event_type=title_changed">К событиям</a>'
                in global_detail.text
            )
            listing = client.get("/sites/1/changes?event_type=title_changed")
            assert "Изменение Title" in listing.text
            href = re.search(
                r'href="(/sites/1/changes/\d+\?event_type=title_changed)">Подробнее</a>',
                listing.text,
            )
            assert href is not None
            title_href = re.search(
                r"Изменение Title.*?href=\"(/sites/1/changes/\d+\?event_type=title_changed)\"",
                listing.text,
                re.DOTALL,
            )
            assert title_href is not None
            detail = client.get(title_href.group(1))
            assert detail.text.index(">Стало<") < detail.text.index(">Было<")
            assert "Стало &lt;точно&gt;" in detail.text
            assert "Было &amp; точно" in detail.text
            assert 'href="/sites/1/changes?event_type=title_changed">К событиям</a>' in detail.text
            detail_path = title_href.group(1)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    before = sha256(database_path.read_bytes()).hexdigest()
    reopened = create_app(settings)
    with TestClient(reopened) as client:
        global_listing = client.get("/changes")
        global_detail = client.get(global_detail_path)
        listing = client.get("/sites/1/changes")
        detail = client.get(detail_path)
        assert global_listing.status_code == global_detail.status_code == 200
        assert listing.status_code == 200
        assert detail.status_code == 200
        assert "Стало &lt;точно&gt;" in detail.text
        assert "Было &amp; точно" in detail.text
    assert sha256(database_path.read_bytes()).hexdigest() == before
