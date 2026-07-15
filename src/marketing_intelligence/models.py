"""Модели локальных данных приложения."""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Site(SQLModel, table=True):
    """Сайт, добавленный пользователем для наблюдения."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AvailabilityCheck(SQLModel, table=True):
    """Сохранённый результат ручной проверки доступности сайта."""

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    completed_at: datetime | None = None
    status: str = Field(index=True)
    message: str
    robots_status: int | None = None
    page_status: int | None = None
