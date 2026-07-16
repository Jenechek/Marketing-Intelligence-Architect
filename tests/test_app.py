from datetime import UTC, datetime
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from marketing_intelligence.config import Settings
from marketing_intelligence.crawl_history import start_crawl_run
from marketing_intelligence.crawler import CrawlSettings
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    AvailabilityCheck,
    CrawlPagePriceRecord,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
)


def build_test_app(tmp_path: Path) -> tuple[FastAPI, Path]:
    data_dir = tmp_path / "data"
    database_path = data_dir / "test.db"
    settings = Settings(
        data_dir=data_dir,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database_path.as_posix()}",
    )
    return create_app(settings), database_path


def add_test_site(client: TestClient) -> None:
    response = client.post(
        "/sites",
        data={"name": "Исходный сайт", "url": "https://example.com/old"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def get_delete_confirmation_token(client: TestClient, site_id: int) -> str:
    response = client.get(f"/sites/{site_id}/delete")
    assert response.status_code == 200
    match = re.search(
        r'name="confirmation_token" value="([^"]+)"',
        response.text,
    )
    assert match is not None
    return match.group(1)


def add_saved_check(app: FastAPI, site_id: int, message: str) -> None:
    with Session(app.state.engine) as session:
        session.add(
            AvailabilityCheck(
                site_id=site_id,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                status="available",
                message=message,
                robots_status=404,
                page_status=200,
            )
        )
        session.commit()


def saved_check_messages(app: FastAPI) -> list[str]:
    with Session(app.state.engine) as session:
        checks = session.exec(select(AvailabilityCheck).order_by(AvailabilityCheck.id))
        return [check.message for check in checks]


def add_saved_crawl(app: FastAPI, site_id: int, page_count: int) -> None:
    with Session(app.state.engine) as session:
        run = CrawlRun(
            site_id=site_id,
            completed_at=datetime.now(UTC),
            status="completed",
            message="Обход сохранён",
            max_pages=10,
            max_depth=2,
            delay=0,
            timeout=5,
            user_agent="DeleteTest/1.0",
            processed=page_count,
            requested=page_count,
            successful=page_count,
            forbidden=0,
            errors=0,
            limited=False,
        )
        session.add(run)
        session.flush()
        for number in range(1, page_count + 1):
            record = CrawlPageRecord(
                crawl_run_id=run.id,
                sequence_number=number,
                url=f"https://example.com/{site_id}/{number}",
                depth=number - 1,
                outcome="html",
                message="Страница обработана",
                http_status=200,
            )
            session.add(record)
            session.flush()
            session.add(
                CrawlPageSnapshot(
                    crawl_page_record_id=record.id,
                    checked_at=datetime.now(UTC),
                    title=None,
                    description="",
                    h1="Тест",
                    normalized_text="тест",
                    content_hash="9f86d081884c7d659a2feaa0c55ad015"
                    "a3bf4f1b2b0b822cd15d6c15b0f00a08",
                    internal_links_json="[]",
                )
            )
            session.add(
                CrawlPagePriceRecord(
                    crawl_page_snapshot_id=record.id,
                    sequence_number=1,
                    amount_text=str(number),
                    currency=None,
                    kind="price",
                    source="test",
                )
            )
        session.commit()


def saved_crawl_data(app: FastAPI) -> tuple[list[int], list[int]]:
    with Session(app.state.engine) as session:
        runs = list(session.exec(select(CrawlRun).order_by(CrawlRun.id)).all())
        pages = list(
            session.exec(select(CrawlPageRecord).order_by(CrawlPageRecord.id)).all()
        )
        return [run.site_id for run in runs], [page.crawl_run_id for page in pages]


def saved_snapshot_site_ids(app: FastAPI) -> list[int]:
    with Session(app.state.engine) as session:
        return list(
            session.exec(
                select(CrawlRun.site_id)
                .join(CrawlPageRecord, CrawlPageRecord.crawl_run_id == CrawlRun.id)
                .join(
                    CrawlPageSnapshot,
                    CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
                )
                .order_by(CrawlPageSnapshot.crawl_page_record_id)
            )
        )


def saved_price_site_ids(app: FastAPI) -> list[int]:
    with Session(app.state.engine) as session:
        return list(
            session.exec(
                select(CrawlRun.site_id)
                .join(CrawlPageRecord, CrawlPageRecord.crawl_run_id == CrawlRun.id)
                .join(
                    CrawlPageSnapshot,
                    CrawlPageSnapshot.crawl_page_record_id == CrawlPageRecord.id,
                )
                .join(
                    CrawlPagePriceRecord,
                    CrawlPagePriceRecord.crawl_page_snapshot_id
                    == CrawlPageSnapshot.crawl_page_record_id,
                )
                .order_by(CrawlPagePriceRecord.id)
            )
        )


def test_site_list_starts_empty_and_initializes_sqlite(tmp_path: Path) -> None:
    app, database_path = build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Сайтов пока нет" in response.text
    assert "Добавить сайт" in response.text
    assert response.text.count('class="primary-action"') == 1
    assert '<button class="primary-action"' in response.text
    assert database_path.is_file()


def test_add_site_and_keep_it_after_restart(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/sites",
            data={"name": "Мой сайт", "url": "https://example.com"},
            follow_redirects=False,
        )

    assert response.status_code == 303

    restarted_app, _ = build_test_app(tmp_path)
    with TestClient(restarted_app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Мой сайт" in response.text
    assert "https://example.com" in response.text


def test_application_startup_recovers_running_crawl_after_restart(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        started = start_crawl_run(app.state.engine, 1, CrawlSettings(delay=0))

    restarted_app, _ = build_test_app(tmp_path)
    with TestClient(restarted_app):
        with Session(restarted_app.state.engine) as session:
            recovered = session.get(CrawlRun, started.id)

    assert recovered is not None
    assert recovered.status == "interrupted"
    assert recovered.completed_at is not None


def test_invalid_url_is_not_saved_and_has_clear_error(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/sites",
            data={"name": "Ошибка", "url": "example.com"},
        )

    assert response.status_code == 422
    assert "Введите полный URL" in response.text
    assert "example.com" in response.text
    assert "Ошибка" in response.text
    assert "Сайтов пока нет" in response.text


def test_edit_form_opens_with_current_values(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        response = client.get("/sites/1/edit")

    assert response.status_code == 200
    assert 'value="Исходный сайт"' in response.text
    assert 'value="https://example.com/old"' in response.text
    assert response.text.count('class="primary-action"') == 1
    assert "Сохранить изменения" in response.text
    assert 'href="/">Отмена</a>' in response.text
    assert "Опасная зона" in response.text
    assert 'href="/sites/1/delete">Удалить сайт</a>' in response.text


def test_edit_site_and_keep_changes_after_restart(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        response = client.post(
            "/sites/1/edit",
            data={"name": "Обновлённый сайт", "url": "https://example.org/new"},
            follow_redirects=False,
        )
        success_response = client.get(response.headers["location"])

    assert response.status_code == 303
    assert response.headers["location"] == "/?updated=1"
    assert success_response.status_code == 200
    assert "Изменения сайта сохранены" in success_response.text

    restarted_app, _ = build_test_app(tmp_path)
    with TestClient(restarted_app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Обновлённый сайт" in response.text
    assert "https://example.org/new" in response.text
    assert "Исходный сайт" not in response.text
    assert "Изменения сайта сохранены" not in response.text
    assert "Редактировать" in response.text


def test_invalid_edit_url_does_not_change_saved_site(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        response = client.post(
            "/sites/1/edit",
            data={"name": "Не сохранять", "url": "example.org/new"},
        )
        saved_response = client.get("/")

    assert response.status_code == 422
    assert "Изменения не сохранены" in response.text
    assert "Введите полный URL" in response.text
    assert 'value="Не сохранять"' in response.text
    assert 'value="example.org/new"' in response.text
    assert "Исходный сайт" in saved_response.text
    assert "https://example.com/old" in saved_response.text
    assert "Не сохранять" not in saved_response.text


def test_cancel_edit_does_not_change_saved_site(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        edit_response = client.get("/sites/1/edit")
        cancel_response = client.get("/")

    assert edit_response.status_code == 200
    assert 'href="/">Отмена</a>' in edit_response.text
    assert cancel_response.status_code == 200
    assert "Исходный сайт" in cancel_response.text
    assert "https://example.com/old" in cancel_response.text


def test_unknown_site_id_has_controlled_russian_error(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        get_response = client.get("/sites/999/edit")
        post_response = client.post(
            "/sites/999/edit",
            data={"name": "Сайт", "url": "https://example.com"},
        )

    assert get_response.status_code == 404
    assert post_response.status_code == 404
    assert "Сайт не найден" in get_response.text
    assert "Не удалось открыть указанный сайт" in post_response.text


def test_delete_confirmation_shows_site_and_warning_without_deleting(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        add_saved_check(app, 1, "Первая проверка")
        add_saved_check(app, 1, "Вторая проверка")
        add_saved_crawl(app, 1, 2)
        add_saved_crawl(app, 1, 1)
        response = client.get("/sites/1/delete")
        saved_response = client.get("/")
        second_token = get_delete_confirmation_token(client, 1)

    assert response.status_code == 200
    assert "Исходный сайт" in response.text
    assert "https://example.com/old" in response.text
    assert "Это действие необратимо" in response.text
    assert "2 записи истории проверок" in response.text
    assert "запусков обхода — 2" in response.text
    assert "записей страниц — 3" in response.text
    assert "страниц с сохранённым содержимым — 3" in response.text
    assert "сохранённых цен — 3" in response.text
    assert 'method="post"' in response.text
    assert 'type="hidden" name="confirmation_token"' in response.text
    first_token = re.search(
        r'name="confirmation_token" value="([^"]+)"',
        response.text,
    )
    assert first_token is not None
    assert first_token.group(1) != second_token
    assert "Исходный сайт" in saved_response.text


def test_cancel_delete_returns_to_edit_without_deleting(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        add_saved_check(app, 1, "История должна сохраниться")
        add_saved_crawl(app, 1, 1)
        confirmation = client.get("/sites/1/delete")
        cancel_response = client.get("/sites/1/edit")
        history = saved_check_messages(app)
        crawl_data = saved_crawl_data(app)

    assert 'href="/sites/1/edit">Отмена</a>' in confirmation.text
    assert cancel_response.status_code == 200
    assert 'value="Исходный сайт"' in cancel_response.text
    assert 'value="https://example.com/old"' in cancel_response.text
    assert history == ["История должна сохраниться"]
    assert crawl_data == ([1], [1])


def test_confirmed_delete_removes_only_selected_site(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        add_saved_check(app, 1, "История выбранного сайта")
        add_saved_crawl(app, 1, 2)
        second_response = client.post(
            "/sites",
            data={"name": "Второй сайт", "url": "https://example.org"},
            follow_redirects=False,
        )
        add_saved_check(app, 2, "История второго сайта")
        add_saved_crawl(app, 2, 1)
        confirmation_token = get_delete_confirmation_token(client, 1)
        response = client.post(
            "/sites/1/delete",
            data={"confirmation_token": confirmation_token},
            follow_redirects=False,
        )
        success_response = client.get(response.headers["location"])
        remaining_history = saved_check_messages(app)
        remaining_crawl_data = saved_crawl_data(app)
        remaining_snapshot_sites = saved_snapshot_site_ids(app)
        remaining_price_sites = saved_price_site_ids(app)

    assert second_response.status_code == 303
    assert response.status_code == 303
    assert response.headers["location"] == "/?deleted=1"
    assert "Сайт окончательно удалён" in success_response.text
    assert "Исходный сайт" not in success_response.text
    assert "Второй сайт" in success_response.text
    assert remaining_history == ["История второго сайта"]
    assert remaining_crawl_data == ([2], [2])
    assert remaining_snapshot_sites == [2]
    assert remaining_price_sites == [2]


def test_deleted_site_stays_absent_after_restart(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        client.post(
            "/sites",
            data={"name": "Сохранённый сайт", "url": "https://example.org/kept"},
        )
        confirmation_token = get_delete_confirmation_token(client, 1)
        response = client.post(
            "/sites/1/delete",
            data={"confirmation_token": confirmation_token},
            follow_redirects=False,
        )

    assert response.status_code == 303

    restarted_app, _ = build_test_app(tmp_path)
    with TestClient(restarted_app) as client:
        saved_response = client.get("/")

    assert saved_response.status_code == 200
    assert "Исходный сайт" not in saved_response.text
    assert "https://example.com/old" not in saved_response.text
    assert "Сохранённый сайт" in saved_response.text
    assert "https://example.org/kept" in saved_response.text


def test_unknown_site_id_delete_has_controlled_russian_error(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        add_saved_check(app, 1, "История другого сайта")
        add_saved_crawl(app, 1, 1)
        get_response = client.get("/sites/999/delete")
        post_response = client.post("/sites/999/delete")
        saved_response = client.get("/")
        history = saved_check_messages(app)
        crawl_data = saved_crawl_data(app)

    assert get_response.status_code == 404
    assert post_response.status_code == 404
    assert "Сайт не найден" in get_response.text
    assert "Не удалось открыть указанный сайт" in post_response.text
    assert "Исходный сайт" in saved_response.text
    assert history == ["История другого сайта"]
    assert crawl_data == ([1], [1])


def test_direct_delete_without_token_is_forbidden_and_keeps_site(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        add_saved_check(app, 1, "История должна сохраниться")
        add_saved_crawl(app, 1, 1)
        response = client.post("/sites/1/delete")
        saved_response = client.get("/")
        history = saved_check_messages(app)
        crawl_data = saved_crawl_data(app)

    assert response.status_code == 403
    assert "Удаление не подтверждено" in response.text
    assert "Подтверждение удаления недействительно" in response.text
    assert "Исходный сайт" in saved_response.text
    assert history == ["История должна сохраниться"]
    assert crawl_data == ([1], [1])


def test_delete_with_invalid_token_is_forbidden_and_keeps_site(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        add_saved_check(app, 1, "История должна сохраниться")
        add_saved_crawl(app, 1, 1)
        response = client.post(
            "/sites/1/delete",
            data={"confirmation_token": "неверный-токен"},
        )
        saved_response = client.get("/")
        history = saved_check_messages(app)
        crawl_data = saved_crawl_data(app)

    assert response.status_code == 403
    assert "Удаление не подтверждено" in response.text
    assert "Исходный сайт" in saved_response.text
    assert history == ["История должна сохраниться"]
    assert crawl_data == ([1], [1])


def test_delete_token_for_another_site_is_forbidden(tmp_path: Path) -> None:
    app, _ = build_test_app(tmp_path)

    with TestClient(app) as client:
        add_test_site(client)
        client.post(
            "/sites",
            data={"name": "Второй сайт", "url": "https://example.org"},
        )
        first_site_token = get_delete_confirmation_token(client, 1)
        response = client.post(
            "/sites/2/delete",
            data={"confirmation_token": first_site_token},
        )
        saved_response = client.get("/")

    assert response.status_code == 403
    assert "Удаление не подтверждено" in response.text
    assert "Исходный сайт" in saved_response.text
    assert "Второй сайт" in saved_response.text
