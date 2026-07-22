"""Подключение и начальная подготовка локальной базы данных."""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine
from sqlalchemy.engine import Engine

from . import models  # noqa: F401 - регистрирует таблицы в SQLModel


def build_engine(database_url: str) -> Engine:
    """Создать движок SQLModel для локальной SQLite."""

    return create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )


def initialize_database(engine: Engine) -> None:
    """Создать отсутствующую базу и известные таблицы."""

    if engine.dialect.name == "sqlite":
        _prepare_sqlite_site_type(engine)
    SQLModel.metadata.create_all(engine)


def _prepare_sqlite_site_type(engine: Engine) -> None:
    """Добавить тип в старую SQLite без пересоздания таблицы и смены ID."""

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "site" not in tables:
        return
    if "site_type" in {column["name"] for column in inspector.get_columns("site")}:
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE site ADD COLUMN site_type VARCHAR(20) "
                "CONSTRAINT ck_site_site_type "
                "CHECK (site_type IN ('competitor', 'owned')) "
                "NOT NULL DEFAULT 'competitor'"
            )
        )
        owned_sources: list[str] = []
        if "gscimport" in tables:
            owned_sources.append(
                "EXISTS (SELECT 1 FROM gscimport WHERE gscimport.site_id = site.id)"
            )
        if "gscpagemetric" in tables:
            owned_sources.append(
                "EXISTS (SELECT 1 FROM gscpagemetric "
                "WHERE gscpagemetric.site_id = site.id)"
            )
        if owned_sources:
            connection.execute(
                text(
                    "UPDATE site SET site_type = 'owned' WHERE "
                    + " OR ".join(owned_sources)
                )
            )
