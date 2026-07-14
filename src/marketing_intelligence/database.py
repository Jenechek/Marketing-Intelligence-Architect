"""Подключение и начальная подготовка локальной базы данных."""

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

    SQLModel.metadata.create_all(engine)
