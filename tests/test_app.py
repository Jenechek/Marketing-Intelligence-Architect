from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing_intelligence.config import Settings
from marketing_intelligence.main import create_app


def build_test_app(tmp_path: Path) -> tuple[FastAPI, Path]:
    data_dir = tmp_path / "data"
    database_path = data_dir / "test.db"
    settings = Settings(
        data_dir=data_dir,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database_path.as_posix()}",
    )
    return create_app(settings), database_path


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
