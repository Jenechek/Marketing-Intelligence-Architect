"""Модели локальных данных приложения."""

from datetime import UTC, datetime

from decimal import Decimal
from fractions import Fraction

from sqlalchemy import CheckConstraint, Column, DateTime, Text, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from .price_persistence import decode_decimal_text


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


class CrawlPagePriceRecord(SQLModel, table=True):
    """Одна обнаруженная цена, связанная со снимком страницы."""

    __table_args__ = (
        UniqueConstraint(
            "crawl_page_snapshot_id",
            "sequence_number",
            name="uq_crawl_page_price_snapshot_sequence",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    crawl_page_snapshot_id: int = Field(
        foreign_key="crawlpagesnapshot.crawl_page_record_id",
        index=True,
    )
    sequence_number: int
    amount_text: str = Field(sa_column=Column(Text, nullable=False))
    currency: str | None = None
    kind: str
    source: str

    @property
    def amount(self) -> Decimal:
        """Восстановить точную сумму из проверенного канонического текста."""

        return decode_decimal_text(self.amount_text)


class SnapshotChangeEvent(SQLModel, table=True):
    """Отдельное обнаруженное изменение между двумя завершёнными снимками."""

    __table_args__ = (
        UniqueConstraint(
            "current_run_id",
            "previous_run_id",
            "event_type",
            "url",
            name="uq_snapshot_change_event_pair_type_url",
        ),
        CheckConstraint(
            "event_type IN ('page_added', 'page_removed', 'title_changed', "
            "'description_changed', 'h1_changed', 'text_changed', "
            "'internal_links_changed')",
            name="ck_snapshot_change_event_type",
        ),
        CheckConstraint(
            "importance IN ('low', 'medium', 'high')",
            name="ck_snapshot_change_event_importance",
        ),
        CheckConstraint(
            "weight >= 1 AND weight <= 3",
            name="ck_snapshot_change_event_weight",
        ),
        CheckConstraint(
            "(change_ratio_numerator IS NULL AND "
            "change_ratio_denominator IS NULL) OR "
            "(change_ratio_numerator >= 0 AND "
            "change_ratio_denominator > 0)",
            name="ck_snapshot_change_event_ratio",
        ),
        CheckConstraint(
            "text_distance IS NULL OR text_distance >= 0",
            name="ck_snapshot_change_event_distance",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    current_run_id: int = Field(foreign_key="crawlrun.id", index=True)
    previous_run_id: int | None = Field(
        default=None,
        foreign_key="crawlrun.id",
        index=True,
    )
    current_page_record_id: int | None = Field(
        default=None,
        foreign_key="crawlpagerecord.id",
        index=True,
    )
    previous_page_record_id: int | None = Field(
        default=None,
        foreign_key="crawlpagerecord.id",
        index=True,
    )
    event_type: str = Field(index=True)
    url: str = Field(index=True)
    current_completed_at: datetime = Field(
        sa_column=Column(UTCDateTime(), nullable=False, index=True),
    )
    importance: str = Field(index=True)
    weight: int = Field(index=True)
    text_distance: int | None = None
    change_ratio_numerator: int | None = None
    change_ratio_denominator: int | None = None

    @property
    def change_ratio(self) -> Fraction | None:
        """Восстановить точную долю изменения, если она применима."""

        if self.change_ratio_numerator is None:
            return None
        if self.change_ratio_denominator is None:
            raise ValueError("Знаменатель точной доли изменения отсутствует.")
        return Fraction(
            self.change_ratio_numerator,
            self.change_ratio_denominator,
        )
