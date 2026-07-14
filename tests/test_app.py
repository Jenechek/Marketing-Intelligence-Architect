from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing_intelligence.config import Settings
from marketing_intelligence.main import create_app


def test_foundation_starts_and_initializes_sqlite(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    database_path = data_dir / "test.db"
    settings = Settings(
        data_dir=data_dir,
        logs_dir=logs_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
    )

    app = create_app(settings)

    assert isinstance(app, FastAPI)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Marketing Intelligence запущен" in response.text
    assert "Система готова" in response.text
    assert data_dir.is_dir()
    assert logs_dir.is_dir()
    assert database_path.is_file()
