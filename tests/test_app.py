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


def add_test_site(client: TestClient) -> None:
    response = client.post(
        "/sites",
        data={"name": "Исходный сайт", "url": "https://example.com/old"},
        follow_redirects=False,
    )
    assert response.status_code == 303


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
