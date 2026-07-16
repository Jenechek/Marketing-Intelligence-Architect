"""Модели локальных данных приложения."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Text
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


class UTCDateTime(TypeDecorator[datetime]):
    """Переносимо сохранять timezone-aware datetime и возвращать его в UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Время снимка должно содержать часовой пояс.")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


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


class CrawlRun(SQLModel, table=True):
    """Сохранённый запуск полного обхода сайта без содержимого страниц."""

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    completed_at: datetime | None = None
    status: str = Field(index=True)
    message: str
    max_pages: int
    max_depth: int
    delay: float
    timeout: float
    user_agent: str
    robots_status: int | None = None
    processed: int | None = None
    requested: int | None = None
    successful: int | None = None
    forbidden: int | None = None
    errors: int | None = None
    limited: bool | None = None


class CrawlPageRecord(SQLModel, table=True):
    """Метаданные одной страницы сохранённого запуска обхода."""

    id: int | None = Field(default=None, primary_key=True)
    crawl_run_id: int = Field(foreign_key="crawlrun.id", index=True)
    sequence_number: int
    url: str
    depth: int
    outcome: str = Field(index=True)
    message: str
    http_status: int | None = None


class CrawlPageSnapshot(SQLModel, table=True):
    """Сохранённое содержимое одной успешно разобранной HTML-страницы."""

    crawl_page_record_id: int = Field(
        foreign_key="crawlpagerecord.id",
        primary_key=True,
    )
    checked_at: datetime = Field(
        sa_column=Column(UTCDateTime(), nullable=False),
    )
    title: str | None = None
    description: str | None = None
    h1: str | None = None
    normalized_text: str = Field(sa_column=Column(Text, nullable=False))
    content_hash: str
    internal_links_json: str = Field(sa_column=Column(Text, nullable=False))
