from datetime import UTC, date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from marketing_intelligence.config import Settings
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.gsc_persistence import save_import
from marketing_intelligence.gsc_csv import ValidatedMetric
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    AvailabilityCheck,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    GSCImport,
    GSCPageMetric,
    Site,
    SiteSchedule,
    SnapshotChangeEvent,
    SITE_TYPE_COMPETITOR,
    SITE_TYPE_OWNED,
)
from marketing_intelligence.sites import add_site, get_site, transfer_site


def _app(tmp_path: Path):
    database = tmp_path / "data" / "task-0036.db"
    settings = Settings(
        data_dir=database.parent,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database.as_posix()}",
    )
    return create_app(settings, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC)), database


def _token(html: str, name: str = "action_token") -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _transfer_form(html: str) -> dict[str, str]:
    return {
        "source_type": re.search(r'name="source_type" value="([^"]+)"', html).group(1),
        "target_type": re.search(r'name="target_type" value="([^"]+)"', html).group(1),
        "action_token": _token(html),
    }


def _seed_event(session: Session, site_id: int, marker: int) -> SnapshotChangeEvent:
    previous_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=marker)
    current_at = previous_at + timedelta(hours=1)
    previous = CrawlRun(
        site_id=site_id,
        started_at=previous_at,
        completed_at=previous_at,
        status="completed",
        message="База",
        max_pages=1,
        max_depth=0,
        delay=0,
        timeout=2,
        user_agent="Task0036/1.0",
    )
    current = CrawlRun(
        site_id=site_id,
        started_at=current_at,
        completed_at=current_at,
        status="completed",
        message="Текущий",
        max_pages=1,
        max_depth=0,
        delay=0,
        timeout=2,
        user_agent="Task0036/1.0",
    )
    session.add(previous)
    session.add(current)
    session.flush()
    url = f"https://example.test/{marker}?value=<unsafe>"
    page = CrawlPageRecord(
        crawl_run_id=current.id,
        sequence_number=1,
        url=url,
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
            checked_at=current_at,
            title="<script>unsafe</script>",
            description=None,
            h1=None,
            normalized_text="unsafe",
            content_hash="0" * 64,
            internal_links_json="[]",
        )
    )
    event = SnapshotChangeEvent(
        current_run_id=current.id,
        previous_run_id=previous.id,
        current_page_record_id=page.id,
        previous_page_record_id=None,
        event_type="page_added",
        url=url,
        current_completed_at=current_at,
        importance="high",
        weight=3,
    )
    session.add(event)
    session.flush()
    return event


def test_new_database_requires_portable_site_type_and_rejects_unknown_value(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    initialize_database(engine)
    columns = {item["name"]: item for item in inspect(engine).get_columns("site")}
    constraints = {item["name"] for item in inspect(engine).get_check_constraints("site")}
    assert columns["site_type"]["nullable"] is False
    assert "ck_site_site_type" in constraints
    competitor = add_site(engine, "Конкурент", "https://competitor.test")
    owned = add_site(engine, "Свой", "https://owned.test", SITE_TYPE_OWNED)
    assert (competitor.site_type, owned.site_type) == ("competitor", "owned")
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO site (name, url, site_type, created_at) "
                "VALUES ('Ошибка', 'https://bad.test', 'unknown', :created_at)"
            ),
            {"created_at": datetime.now(UTC)},
        )
        session.commit()
    engine.dispose()


def test_old_sqlite_migration_classifies_sites_keeps_ids_data_and_is_idempotent(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    old = MetaData()
    Table(
        "site",
        old,
        Column("id", Integer, primary_key=True),
        Column("name", String, nullable=False),
        Column("url", String, nullable=False),
        Column("created_at", DateTime, nullable=False),
    )
    old.create_all(engine)
    SQLModel.metadata.create_all(
        engine,
        tables=[table for table in SQLModel.metadata.sorted_tables if table.name != "site"],
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO site (id, name, url, created_at) VALUES "
                "(10, 'С GSC', 'https://owned.test', :now), "
                "(20, 'Без GSC', 'https://competitor.test', :now), "
                "(30, 'Только метрики', 'https://metric.test', :now)"
            ),
            {"now": datetime(2026, 1, 1)},
        )
    with Session(engine) as session:
        session.add(
            GSCImport(
                id=101,
                site_id=10,
                filename="Pages.csv",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                imported_at=datetime(2026, 2, 1, tzinfo=UTC),
                row_count=0,
                added_count=0,
                updated_count=0,
                unchanged_count=0,
                delimiter=",",
            )
        )
        session.add(
            AvailabilityCheck(
                id=202,
                site_id=20,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime(2026, 1, 1, tzinfo=UTC),
                status="available",
                message="Сохранено",
            )
        )
        session.add(
            GSCPageMetric(
                id=102,
                site_id=30,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                normalized_url="https://metric.test/a",
                clicks=1,
                impressions=2,
                last_import_id=101,
                updated_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        session.add(
            SiteSchedule(
                id=404,
                site_id=20,
                enabled=False,
                frequency="weekly",
                local_weekday=0,
                local_time="09:00",
                max_pages=1,
                max_depth=0,
                delay=0,
                timeout=2,
                user_agent="Task0036/1.0",
            )
        )
        session.add(
            CrawlRun(
                id=303,
                site_id=20,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime(2026, 1, 1, tzinfo=UTC),
                status="completed",
                message="Сохранено",
                max_pages=1,
                max_depth=0,
                delay=0,
                timeout=2,
                user_agent="Task0036/1.0",
            )
        )
        session.commit()

    initialize_database(engine)
    initialize_database(engine)
    with Session(engine) as session:
        sites = session.exec(select(Site).order_by(Site.id)).all()
        assert [(item.id, item.site_type) for item in sites] == [
            (10, SITE_TYPE_OWNED),
            (20, SITE_TYPE_COMPETITOR),
            (30, SITE_TYPE_OWNED),
        ]
        assert session.get(GSCImport, 101).site_id == 10
        assert session.get(AvailabilityCheck, 202).site_id == 20
        assert session.get(CrawlRun, 303).site_id == 20
        assert session.get(GSCPageMetric, 102).site_id == 30
        assert session.get(SiteSchedule, 404).site_id == 20
    assert "ck_site_site_type" in {
        item["name"] for item in inspect(engine).get_check_constraints("site")
    }
    engine.dispose()


def test_sections_creation_isolation_xss_and_actual_return_links(tmp_path: Path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        competitor = client.post(
            "/competitors",
            data={"name": "<script>конкурент</script>", "url": "https://competitor.test"},
            follow_redirects=False,
        )
        owned = client.post(
            "/own-sites",
            data={"name": "Свой сайт", "url": "https://owned.test"},
            follow_redirects=False,
        )
        competitors = client.get("/competitors")
        own_sites = client.get("/own-sites")
        edited = client.post(
            "/sites/2/edit",
            data={"name": "Свой обновлён", "url": "https://owned.test/new"},
            follow_redirects=False,
        )
        delete_screen = client.get("/sites/2/delete")
        deleted = client.post(
            "/sites/2/delete",
            data={"confirmation_token": _token(delete_screen.text, "confirmation_token")},
            follow_redirects=False,
        )
    assert root.status_code == 307 and root.headers["location"] == "/competitors"
    assert competitor.headers["location"] == "/competitors?created=1"
    assert owned.headers["location"] == "/own-sites?created=1"
    assert "&lt;script&gt;конкурент&lt;/script&gt;" in competitors.text
    assert "<script>конкурент</script>" not in competitors.text
    assert "Свой сайт" not in competitors.text
    assert "Импорт Search Console" not in competitors.text
    assert "Свой сайт" in own_sites.text
    assert "<script>конкурент</script>" not in own_sites.text
    assert "Импорт Search Console" in own_sites.text
    assert edited.headers["location"] == "/own-sites?updated=1"
    assert deleted.headers["location"] == "/own-sites?deleted=1"


def test_gsc_is_owned_only_and_preview_is_rechecked_after_transfer(tmp_path: Path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/competitors",
            data={"name": "Конкурент", "url": "https://competitor.test"},
        )
        client.post(
            "/own-sites",
            data={"name": "Свой", "url": "https://owned.test"},
        )
        assert client.get("/own-sites/1/imports").status_code == 404
        assert client.get("/sites/1/imports").status_code == 404
        assert client.get("/own-sites/1/gsc-pages").status_code == 404
        assert client.post("/own-sites/1/imports/preview").status_code == 404

        screen = client.get("/own-sites/2/imports")
        preview = client.post(
            "/own-sites/2/imports/preview",
            data={
                "action_token": _token(screen.text),
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "report_confirmed": "yes",
            },
            files={
                "csv_file": (
                    "Pages.csv",
                    b"Page,Clicks,Impressions\nhttps://owned.test/a,1,2\n",
                    "text/csv",
                )
            },
        )
        transfer_screen = client.get("/sites/2/transfer")
        moved = client.post(
            "/sites/2/transfer",
            data=_transfer_form(transfer_screen.text),
            follow_redirects=False,
        )
        confirm = client.post(
            "/own-sites/2/imports/confirm",
            data={
                "preview_token": _token(preview.text, "preview_token"),
                "action_token": _token(preview.text),
                "page": "0",
                "clicks": "1",
                "impressions": "2",
                "position": "",
            },
        )
    assert moved.headers["location"] == "/competitors?transferred=1"
    assert confirm.status_code == 404
    with Session(app.state.engine) as session:
        assert session.exec(select(GSCImport)).all() == []
        assert session.exec(select(GSCPageMetric)).all() == []


def test_transfer_is_protected_atomic_preserves_relations_and_blocks_gsc(tmp_path: Path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/competitors",
            data={"name": "Переносимый", "url": "https://move.test"},
        )
        client.post(
            "/own-sites",
            data={"name": "С данными", "url": "https://gsc.test"},
        )
        with Session(app.state.engine) as session:
            session.add(
                AvailabilityCheck(
                    id=50,
                    site_id=1,
                    started_at=datetime.now(UTC),
                    status="available",
                    message="Сохранено",
                )
            )
            session.add(
                SiteSchedule(
                    id=60,
                    site_id=1,
                    enabled=False,
                    frequency="weekly",
                    local_weekday=0,
                    local_time="09:00",
                    max_pages=1,
                    max_depth=0,
                    delay=0,
                    timeout=2,
                    user_agent="Task0036/1.0",
                )
            )
            session.add(
                CrawlRun(
                    id=70,
                    site_id=1,
                    started_at=datetime.now(UTC),
                    status="running",
                    message="Выполняется",
                    max_pages=1,
                    max_depth=0,
                    delay=0,
                    timeout=2,
                    user_agent="Task0036/1.0",
                )
            )
            session.commit()
        save_import(
            app.state.engine,
            site_id=2,
            filename="Pages.csv",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            delimiter=",",
            metrics=(ValidatedMetric("https://gsc.test/a", 1, 2, None),),
        )

        before_get = sha256((tmp_path / "data" / "task-0036.db").read_bytes()).hexdigest()
        first_screen = client.get("/sites/1/transfer")
        assert sha256((tmp_path / "data" / "task-0036.db").read_bytes()).hexdigest() == before_get
        first_form = _transfer_form(first_screen.text)
        moved = client.post("/sites/1/transfer", data=first_form, follow_redirects=False)
        replay = client.post("/sites/1/transfer", data=first_form, follow_redirects=False)
        back_screen = client.get("/sites/1/transfer")
        moved_back = client.post(
            "/sites/1/transfer",
            data=_transfer_form(back_screen.text),
            follow_redirects=False,
        )
        blocked_screen = client.get("/sites/2/transfer")
        blocked = client.post(
            "/sites/2/transfer",
            data=_transfer_form(blocked_screen.text),
            follow_redirects=False,
        )
        foreign = client.post(
            "/sites/2/transfer",
            data={**_transfer_form(blocked_screen.text), "action_token": first_form["action_token"]},
        )
        missing = client.get("/sites/999/transfer")

    assert moved.headers["location"] == "/own-sites?transferred=1"
    assert replay.status_code == 409
    assert moved_back.headers["location"] == "/competitors?transferred=1"
    assert blocked.status_code == 409
    assert "импорты или показатели Search Console" in blocked.text
    assert foreign.status_code == 403
    assert missing.status_code == 404
    with Session(app.state.engine) as session:
        assert session.get(Site, 1).site_type == SITE_TYPE_COMPETITOR
        assert session.get(Site, 2).site_type == SITE_TYPE_OWNED
        assert session.get(AvailabilityCheck, 50).site_id == 1
        assert session.get(SiteSchedule, 60).site_id == 1
        assert session.get(CrawlRun, 70).status == "running"
        assert session.exec(select(GSCImport).where(GSCImport.site_id == 2)).one()


def test_concurrent_transfer_submission_changes_type_once(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}")
    initialize_database(engine)
    site = add_site(engine, "Однократно", "https://once.test")

    def submit():
        return transfer_site(
            engine,
            site.id,
            SITE_TYPE_COMPETITOR,
            SITE_TYPE_OWNED,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: submit(), range(2)))
    assert sum(item is not None for item in results) == 1
    assert get_site(engine, site.id).site_type == SITE_TYPE_OWNED
    engine.dispose()


def test_global_histories_filters_exports_and_gets_are_isolated_and_read_only(tmp_path: Path):
    app, database = _app(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/competitors",
            data={"name": "Конкурент история", "url": "https://competitor.test"},
        )
        client.post(
            "/own-sites",
            data={"name": "Свой история", "url": "https://owned.test"},
        )
        with Session(app.state.engine) as session:
            competitor_event = _seed_event(session, 1, 1)
            owned_event = _seed_event(session, 2, 2)
            session.commit()
            competitor_event_id = competitor_event.id
            owned_event_id = owned_event.id

        before = sha256(database.read_bytes()).hexdigest()
        competitors = client.get("/competitors/changes")
        owned = client.get("/own-sites/changes")
        forged_filter = client.get("/competitors/changes?site_id=2")
        forged_detail = client.get(
            f"/sites/2/changes/{owned_event_id}?scope=competitor"
        )
        competitor_json = client.get("/competitors/changes/export.json").json()
        owned_json = client.get("/own-sites/changes/export.json").json()
        client.get(f"/sites/1/changes/{competitor_event_id}")
        after = sha256(database.read_bytes()).hexdigest()

    assert "Конкурент история" in competitors.text
    assert "Свой история" not in competitors.text
    assert "Свой история" in owned.text
    assert "Конкурент история" not in owned.text
    assert "&lt;unsafe&gt;" in competitors.text
    assert forged_filter.status_code == 404
    assert "Свой история" not in forged_filter.text
    assert forged_detail.status_code == 404
    assert [item["site_id"] for item in competitor_json["events"]] == [1]
    assert [item["site_id"] for item in owned_json["events"]] == [2]
    assert before == after
