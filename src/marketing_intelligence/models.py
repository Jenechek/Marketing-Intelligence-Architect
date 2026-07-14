"""Модели локальных данных приложения."""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Site(SQLModel, table=True):
    """Сайт, добавленный пользователем для наблюдения."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
