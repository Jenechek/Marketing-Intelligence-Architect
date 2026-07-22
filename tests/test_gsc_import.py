from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect
from sqlmodel import Session, SQLModel, select

from marketing_intelligence.config import Settings
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.gsc_csv import (
    GSCImportError,
    MAX_FILE_BYTES,
    automatic_mapping,
    parse_mapping,
    parse_pages_csv,
    read_limited_upload,
    validate_period,
    validate_rows,
    ValidatedMetric,
)
from marketing_intelligence.gsc_persistence import save_import
from marketing_intelligence.gsc_query import list_metrics
from marketing_intelligence.gsc_preview import PREVIEW_TTL, PreviewStore
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    CrawlPageRecord,
    CrawlRun,
    GSCImport,
    GSCPageMetric,
)


def build_app(tmp_path: Path, *, now_provider=None):
    database = tmp_path / "data" / "test.db"
    settings = Settings(
        data_dir=database.parent,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database.as_posix()}",
    )
    return create_app(settings, now_provider=now_provider), database


def token(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def add_site(client: TestClient, name="Сайт", url="https://example.com/") -> int:
    response = client.post(
        "/own-sites", data={"name": name, "url": url}, follow_redirects=False
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("=")[-1]) if "site_id" in response.headers["location"] else 1


def upload_preview(
    client: TestClient,
    site_id: int = 1,
    csv_text: str = "Page,Clicks,Impressions,Position\nhttps://example.com/a,2,10,1.25\n",
):
    screen = client.get(f"/own-sites/{site_id}/imports")
    upload_token = token(screen.text, "action_token")
    return client.post(
        f"/own-sites/{site_id}/imports/preview",
        data={
            "action_token": upload_token,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "report_confirmed": "yes",
        },
        files={"csv_file": ("Pages.csv", csv_text.encode("utf-8"), "text/csv")},
    )


def confirm_preview(client: TestClient, response, site_id: int = 1, mapping=None):
    data = {
        "preview_token": token(response.text, "preview_token"),
        "action_token": token(response.text, "action_token"),
        "page": "0",
        "clicks": "1",
        "impressions": "2",
        "position": "3",
    }
    data.update(mapping or {})
    return client.post(
        f"/own-sites/{site_id}/imports/confirm", data=data, follow_redirects=False
    )


@pytest.mark.parametrize(
    ("delimiter", "bom"),
    [(bom_delimiter, bom) for bom_delimiter in (",", ";", "\t") for bom in (False, True)],
)
def test_csv_sniffer_supports_delimiters_bom_service_and_blank_lines(delimiter, bom):
    text = (
        "Служебная строка\n\n"
        + delimiter.join(("Страницы", "Клики", "Показы", "Средняя позиция"))
        + "\n"
        + delimiter.join(("https://example.com/a", "2", "10", "1.250"))
        + "\n\n"
    )
    content = text.encode("utf-8")
    if bom:
        content = b"\xef\xbb\xbf" + content
    parsed = parse_pages_csv("Pages.csv", content)
    assert parsed.delimiter == delimiter
    assert parsed.rows[0].source_line == 4
    assert dict(parsed.automatic_mapping) == {
        "page": 0,
        "clicks": 1,
        "impressions": 2,
        "position": 3,
    }


def test_mapping_rejects_ambiguity_missing_and_reused_column():
    assert automatic_mapping(("Page", "URL", "Clicks", "Impressions"))["page"] is None
    mapping, errors = parse_mapping(
        {"page": "0", "clicks": "0", "impressions": "", "position": ""}, 3
    )
    assert mapping["impressions"] is None
    assert errors == {
        "impressions": "Выберите столбец.",
        "mapping": "Каждому полю нужен отдельный столбец.",
    }


def test_unknown_headers_can_be_mapped_manually_and_extra_columns_are_ignored():
    parsed = parse_pages_csv(
        "custom.csv",
        b"Address,Visits,Views,Rank,Ignored\nhttps://example.com/a,1,4,2.125,x\n",
    )
    assert all(value is None for _, value in parsed.automatic_mapping)
    mapping, errors = parse_mapping(
        {"page": "0", "clicks": "1", "impressions": "2", "position": "3"}, 5
    )
    assert errors == {}
    result = validate_rows(parsed, mapping, "https://example.com")
    assert result.error_count == 0
    assert result.metrics[0].average_position_text == "2.125"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xff", "UTF-8"),
        (b"Page,Clicks,Impressions\nhttps://example.com/\x00,1,2", "нулевой"),
        (b"Page|Clicks|Impressions\nhttps://example.com/|1|2", "разделитель"),
        (b"Page,Clicks,Impressions\n\"https://example.com/,1,2", "структуру"),
    ],
)
def test_csv_rejects_invalid_encoding_nul_delimiter_and_structure(content, message):
    with pytest.raises(GSCImportError, match=message):
        parse_pages_csv("Pages.csv", content)


def test_stream_limit_reads_only_limit_plus_one():
    from io import BytesIO

    with pytest.raises(GSCImportError, match="5 МиБ"):
        read_limited_upload(BytesIO(b"x" * (MAX_FILE_BYTES + 1)))


def test_csv_enforces_row_column_header_filename_and_value_limits():
    too_many_columns = ",".join(["Page", "Clicks", "Impressions"] + [f"x{i}" for i in range(48)])
    with pytest.raises(GSCImportError, match="50 столбцов"):
        parse_pages_csv("Pages.csv", f"{too_many_columns}\n".encode())
    with pytest.raises(GSCImportError, match="Имя файла"):
        parse_pages_csv("x" * 252 + ".csv", b"Page,Clicks,Impressions\n/a,1,2")
    with pytest.raises(GSCImportError, match="Название столбца"):
        parse_pages_csv(
            "Pages.csv", ("Page,Clicks," + "x" * 201 + "\nhttps://example.com,1,2").encode()
        )
    with pytest.raises(GSCImportError, match="значение слишком длинное"):
        parse_pages_csv(
            "Pages.csv",
            ("Page,Clicks,Impressions,Extra\nhttps://example.com,1,2," + "x" * 4097).encode(),
        )
    rows = "".join(f"https://example.com/{index},1,2\n" for index in range(10_001))
    with pytest.raises(GSCImportError, match="10 000"):
        parse_pages_csv("Pages.csv", ("Page,Clicks,Impressions\n" + rows).encode())


def test_row_validation_is_exact_and_reports_all_core_errors():
    parsed = parse_pages_csv(
        "Pages.csv",
        (
            "Page,Clicks,Impressions,Position\n"
            "https://example.com/a#one,2,10,1.2300\n"
            "https://example.com/a#two,11,10,NaN\n"
            "https://other.example/a,-1,x,0\n"
        ).encode(),
    )
    mapping = {"page": 0, "clicks": 1, "impressions": 2, "position": 3}
    result = validate_rows(parsed, mapping, "https://example.com/")
    assert result.metrics[0].average_position_text == "1.23"
    assert result.error_count == 7
    assert any("совпадает после нормализации" in item for item in result.errors)
    assert any("другому origin" in item for item in result.errors)


def test_optional_position_and_zero_impressions_keep_exact_values():
    parsed = parse_pages_csv(
        "Pages.csv", b"Page,Clicks,Impressions,Position\nhttps://example.com/a,0,0,\n"
    )
    result = validate_rows(
        parsed, {"page": 0, "clicks": 1, "impressions": 2, "position": 3}, "https://example.com"
    )
    assert result.error_count == 0
    assert result.metrics[0].average_position_text is None


def test_period_validation_rejects_missing_reverse_and_future():
    _, _, missing = validate_period("", "", date(2026, 1, 31))
    assert set(missing) == {"period_start", "period_end"}
    _, _, reverse = validate_period("2026-01-20", "2026-01-10", date(2026, 1, 31))
    assert "не позже" in reverse["period_start"]
    _, _, future = validate_period("2026-01-01", "2026-02-01", date(2026, 1, 31))
    assert "будущем" in future["period_end"]


def test_preview_store_expires_is_site_bound_and_is_single_use():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    parsed = parse_pages_csv(
        "Pages.csv", b"Page,Clicks,Impressions\nhttps://example.com/a,1,2\n"
    )
    clock = lambda: now
    store = PreviewStore(now_provider=clock)
    preview = store.add(1, date(2026, 1, 1), date(2026, 1, 1), parsed)
    assert store.get(preview.token, 2) is None
    assert store.acquire(preview.token, 1) is not None
    assert store.acquire(preview.token, 1) is None
    store.release(preview.token)
    now += PREVIEW_TTL
    assert store.get(preview.token, 1) is None


def test_web_import_preview_confirm_repeat_metrics_and_history(tmp_path: Path):
    app, _ = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(app) as client:
        add_site(client)
        first_preview = upload_preview(client)
        assert first_preview.status_code == 200, first_preview.text
        assert "Сопоставление столбцов" in first_preview.text
        assert "https://example.com/a" in first_preview.text
        first_confirm = confirm_preview(client, first_preview)
        assert first_confirm.status_code == 303

        repeated = upload_preview(
            client,
            csv_text=(
                "Page,Clicks,Impressions,Position\n"
                "https://example.com/a,3,10,1.25\n"
                "https://example.com/b,0,0,\n"
            ),
        )
        repeated_confirm = confirm_preview(client, repeated)
        assert repeated_confirm.status_code == 303
        duplicate = confirm_preview(client, repeated)
        assert duplicate.status_code == 409

        history = client.get("/sites/1/imports")
        metrics = client.get("/sites/1/gsc-pages")
        assert history.text.count("<td>Pages.csv</td>") == 2
        assert "добавлено 1, обновлено 1, без изменений 0" in history.text
        assert "30.00 %" in metrics.text
        assert "0.00 %" in metrics.text
        assert "1.25" in metrics.text
        assert "Подходящего обхода пока нет" in metrics.text

        with Session(app.state.engine) as session:
            imports = session.exec(select(GSCImport).order_by(GSCImport.id)).all()
            rows = session.exec(select(GSCPageMetric).order_by(GSCPageMetric.normalized_url)).all()
            assert len(imports) == 2
            assert len(rows) == 2
            assert rows[0].clicks == 3
            assert rows[0].average_position_text == "1.25"
            assert rows[1].average_position_text is None


def test_mapping_error_and_bad_rows_preserve_preview_without_writes(tmp_path: Path):
    app, _ = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(app) as client:
        add_site(client)
        preview = upload_preview(client, csv_text="Page,Clicks,Impressions\nhttps://example.com/a,5,2\n")
        bad_mapping = confirm_preview(
            client, preview, mapping={"clicks": "0", "position": ""}
        )
        assert bad_mapping.status_code == 422
        assert "Каждому полю нужен отдельный столбец" in bad_mapping.text
        row_error = confirm_preview(client, bad_mapping, mapping={"clicks": "1", "position": ""})
        assert row_error.status_code == 422
        assert "клики не могут превышать показы" in row_error.text
        with Session(app.state.engine) as session:
            assert session.exec(select(GSCImport)).all() == []
            assert session.exec(select(GSCPageMetric)).all() == []


def test_upload_post_is_protected_and_token_is_bound_to_site(tmp_path: Path):
    app, _ = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(app) as client:
        add_site(client, name="Первый")
        add_site(client, name="Второй", url="https://second.example")
        first_screen = client.get("/sites/1/imports")
        response = client.post(
            "/own-sites/2/imports/preview",
            data={
                "action_token": token(first_screen.text, "action_token"),
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "report_confirmed": "yes",
            },
            files={
                "csv_file": (
                    "Pages.csv",
                    b"Page,Clicks,Impressions\nhttps://second.example/a,1,2\n",
                    "text/csv",
                )
            },
        )
        assert response.status_code == 403
        assert client.post("/own-sites/1/imports/preview").status_code in {403, 422}


def test_metrics_match_latest_completed_or_partial_crawl_in_batch(tmp_path: Path):
    app, _ = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(app) as client:
        add_site(client)
        preview = upload_preview(
            client,
            csv_text=(
                "Page,Clicks,Impressions,Position\n"
                "https://example.com/a,2,10,1\n"
                "https://example.com/missing,1,2,2\n"
            ),
        )
        assert confirm_preview(client, preview).status_code == 303
        with Session(app.state.engine) as session:
            run = CrawlRun(
                site_id=1,
                started_at=datetime(2026, 1, 31, tzinfo=UTC),
                completed_at=datetime(2026, 1, 31, tzinfo=UTC),
                status="partial",
                message="частичный",
                max_pages=10,
                max_depth=2,
                delay=0,
                timeout=5,
                user_agent="test",
            )
            session.add(run)
            session.flush()
            session.add(CrawlPageRecord(crawl_run_id=run.id, sequence_number=1, url="https://example.com/a", depth=1, outcome="html", message="ok", http_status=200))
            session.commit()
        response = client.get("/sites/1/gsc-pages")
        assert "Есть в последнем обходе" in response.text
        assert "Не найдена в последнем обходе" in response.text
        assert "не доказывает, что страница сиротская" in response.text


def test_import_views_are_read_only_escaped_and_delete_is_transactional(tmp_path: Path):
    app, database = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(app) as client:
        add_site(client)
        preview = upload_preview(
            client,
            csv_text='Page,Clicks,Impressions,Position,Extra\nhttps://example.com/a,1,2,1,"<script>alert(1)</script>"\n',
        )
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in preview.text
        assert "<script>alert(1)</script>" not in preview.text
        assert confirm_preview(client, preview).status_code == 303
        before = sha256(database.read_bytes()).hexdigest()
        client.get("/sites/1/imports")
        client.get("/sites/1/gsc-pages")
        assert sha256(database.read_bytes()).hexdigest() == before
        delete_screen = client.get("/sites/1/delete")
        assert "импортов Search Console — 1" in delete_screen.text
        delete_response = client.post(
            "/sites/1/delete",
            data={"confirmation_token": token(delete_screen.text, "confirmation_token")},
            follow_redirects=False,
        )
        assert delete_response.status_code == 303
        with Session(app.state.engine) as session:
            assert session.exec(select(GSCImport)).all() == []
            assert session.exec(select(GSCPageMetric)).all() == []


def test_preview_is_lost_after_application_restart(tmp_path: Path):
    app, _ = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(app) as client:
        add_site(client)
        preview = upload_preview(client)
        preview_token = token(preview.text, "preview_token")
        action_token = token(preview.text, "action_token")
    restarted, _ = build_app(tmp_path, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    with TestClient(restarted) as client:
        response = client.post(
            "/own-sites/1/imports/confirm",
            data={
                "preview_token": preview_token,
                "action_token": action_token,
                "page": "0",
                "clicks": "1",
                "impressions": "2",
                "position": "3",
            },
        )
    assert response.status_code in {403, 409}


def test_transaction_rolls_back_log_and_metrics_on_unique_failure(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        add_site(client)
        duplicate = ValidatedMetric("https://example.com/a", 1, 2, "1")
        with pytest.raises(Exception):
            save_import(
                app.state.engine,
                site_id=1,
                filename="Pages.csv",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                delimiter=",",
                metrics=(duplicate, duplicate),
            )
        with Session(app.state.engine) as session:
            assert session.exec(select(GSCImport)).all() == []
            assert session.exec(select(GSCPageMetric)).all() == []


def test_old_sqlite_gets_only_two_gsc_tables_and_keeps_existing_data(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    old_tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.name not in {"gscimport", "gscpagemetric"}
    ]
    SQLModel.metadata.create_all(engine, tables=old_tables)
    with Session(engine) as session:
        from marketing_intelligence.models import Site

        session.add(Site(name="Старый сайт", url="https://old.example"))
        session.commit()
    before = set(inspect(engine).get_table_names())
    initialize_database(engine)
    after = set(inspect(engine).get_table_names())
    assert after - before == {"gscimport", "gscpagemetric"}
    with Session(engine) as session:
        assert session.exec(select(Site)).one().name == "Старый сайт"


def test_history_and_metrics_have_stable_server_pagination(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        add_site(client)
        metrics = tuple(
            ValidatedMetric(f"https://example.com/{index:02d}", index, 100, None)
            for index in range(21)
        )
        save_import(
            app.state.engine,
            site_id=1,
            filename="seed.csv",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            delimiter=",",
            metrics=metrics,
        )
        for index in range(1, 21):
            save_import(
                app.state.engine,
                site_id=1,
                filename=f"repeat-{index}.csv",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                delimiter=",",
                metrics=(metrics[0],),
            )
        first_history = client.get("/sites/1/imports?page=1")
        second_history = client.get("/sites/1/imports?page=2")
        assert first_history.text.count("repeat-") == 20
        assert "seed.csv" in second_history.text
        first_metrics = client.get(
            "/sites/1/gsc-pages?period_start=2026-01-01&period_end=2026-01-31&page=1"
        )
        second_metrics = client.get(
            "/sites/1/gsc-pages?period_start=2026-01-01&period_end=2026-01-31&page=2"
        )
        assert "https://example.com/00" in first_metrics.text
        assert "https://example.com/19" in first_metrics.text
        assert "https://example.com/20" not in first_metrics.text
        assert "https://example.com/20" in second_metrics.text
        assert client.get("/sites/1/imports?page=0").status_code == 422
        assert client.get("/sites/1/gsc-pages?page=bad").status_code == 422


def test_metric_crawler_matching_uses_fixed_query_count(tmp_path: Path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        add_site(client)
        metrics = tuple(
            ValidatedMetric(f"https://example.com/{index}", 1, 2, None)
            for index in range(20)
        )
        save_import(
            app.state.engine,
            site_id=1,
            filename="Pages.csv",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            delimiter=",",
            metrics=metrics,
        )
        with Session(app.state.engine) as session:
            run = CrawlRun(
                site_id=1,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                status="completed",
                message="ok",
                max_pages=20,
                max_depth=1,
                delay=0,
                timeout=1,
                user_agent="test",
            )
            session.add(run)
            session.flush()
            for index in range(20):
                session.add(
                    CrawlPageRecord(
                        crawl_run_id=run.id,
                        sequence_number=index + 1,
                        url=f"https://example.com/{index}",
                        depth=1,
                        outcome="html",
                        message="ok",
                        http_status=200,
                    )
                )
            session.commit()
        statements = []

        def track(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(app.state.engine, "before_cursor_execute", track)
        try:
            result = list_metrics(
                app.state.engine, 1, date(2026, 1, 1), date(2026, 1, 31), 1
            )
        finally:
            event.remove(app.state.engine, "before_cursor_execute", track)
        assert len(result.items) == 20
        assert len(statements) == 4
