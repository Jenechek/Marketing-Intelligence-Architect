"""Модели локальных данных приложения."""

from datetime import UTC, date, datetime

from decimal import Decimal
from fractions import Fraction

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from .price_persistence import decode_decimal_text


SITE_TYPE_COMPETITOR = "competitor"
SITE_TYPE_OWNED = "owned"
SITE_TYPES = (SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED)


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

    __table_args__ = (
        CheckConstraint(
            "site_type IN ('competitor', 'owned')",
            name="ck_site_site_type",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str
    site_type: str = Field(
        default=SITE_TYPE_COMPETITOR,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=SITE_TYPE_COMPETITOR,
            index=True,
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SiteSchedule(SQLModel, table=True):
    """Сохраняемое правило автоматического полного обхода одного сайта."""

    __table_args__ = (
        UniqueConstraint("site_id", name="uq_site_schedule_site"),
        CheckConstraint(
            "frequency IN ('daily', 'weekly')",
            name="ck_site_schedule_frequency",
        ),
        CheckConstraint(
            "local_weekday >= 0 AND local_weekday <= 6",
            name="ck_site_schedule_weekday",
        ),
        CheckConstraint("max_pages >= 1", name="ck_site_schedule_max_pages"),
        CheckConstraint("max_depth >= 0", name="ck_site_schedule_max_depth"),
        CheckConstraint("delay >= 0", name="ck_site_schedule_delay"),
        CheckConstraint("timeout > 0", name="ck_site_schedule_timeout"),
    )

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    enabled: bool = Field(sa_column=Column(Boolean, nullable=False, default=False))
    frequency: str = Field(default="weekly")
    local_weekday: int
    local_time: str = Field(default="09:00")
    next_run_at: datetime | None = Field(
        default=None,
        sa_column=Column(UTCDateTime(), nullable=True, index=True),
    )
    max_pages: int
    max_depth: int
    delay: float
    timeout: float
    user_agent: str
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(UTCDateTime(), nullable=False),
    )


class ScheduledCrawlEntry(SQLModel, table=True):
    """Журнал автоматических запусков и сохраняемая последовательная очередь."""

    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_scheduled_crawl_entry_schedule_moment",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'deferred', "
            "'failed', 'interrupted', 'missed', 'cancelled')",
            name="ck_scheduled_crawl_entry_status",
        ),
        CheckConstraint(
            "notification_status IN ('not_applicable', 'disabled', 'pending', "
            "'sent', 'failed')",
            name="ck_scheduled_crawl_entry_notification",
        ),
        CheckConstraint(
            "missed_periods >= 0",
            name="ck_scheduled_crawl_entry_missed_periods",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    schedule_id: int | None = Field(
        default=None,
        foreign_key="siteschedule.id",
        index=True,
    )
    scheduled_for: datetime = Field(
        sa_column=Column(UTCDateTime(), nullable=False, index=True),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(UTCDateTime(), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(UTCDateTime(), nullable=True),
    )
    status: str = Field(index=True)
    message: str = Field(sa_column=Column(Text, nullable=False))
    crawl_run_id: int | None = Field(
        default=None,
        foreign_key="crawlrun.id",
        index=True,
    )
    max_pages: int
    max_depth: int
    delay: float
    timeout: float
    user_agent: str
    retry_of_id: int | None = Field(
        default=None,
        foreign_key="scheduledcrawlentry.id",
        index=True,
    )
    missed_periods: int = Field(default=0)
    notification_status: str = Field(default="not_applicable")


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


class GSCImport(SQLModel, table=True):
    """Подтверждённый импорт отчёта Search Console «Страницы»."""

    __table_args__ = (
        CheckConstraint("source_type = 'gsc_pages'", name="ck_gsc_import_source_type"),
        CheckConstraint("period_start <= period_end", name="ck_gsc_import_period"),
        CheckConstraint("row_count >= 0", name="ck_gsc_import_row_count"),
        CheckConstraint("added_count >= 0", name="ck_gsc_import_added_count"),
        CheckConstraint("updated_count >= 0", name="ck_gsc_import_updated_count"),
        CheckConstraint("unchanged_count >= 0", name="ck_gsc_import_unchanged_count"),
        CheckConstraint(
            "delimiter IN (',', ';', '\t')",
            name="ck_gsc_import_delimiter",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    source_type: str = Field(default="gsc_pages", index=True)
    filename: str = Field(sa_column=Column(Text, nullable=False))
    period_start: date = Field(sa_column=Column(Date, nullable=False, index=True))
    period_end: date = Field(sa_column=Column(Date, nullable=False, index=True))
    imported_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(UTCDateTime(), nullable=False, index=True),
    )
    row_count: int
    added_count: int
    updated_count: int
    unchanged_count: int
    delimiter: str


class GSCPageMetric(SQLModel, table=True):
    """Текущие показатели страницы Search Console за один период."""

    __table_args__ = (
        UniqueConstraint(
            "site_id",
            "period_start",
            "period_end",
            "normalized_url",
            name="uq_gsc_page_metric_site_period_url",
        ),
        CheckConstraint("period_start <= period_end", name="ck_gsc_page_metric_period"),
        CheckConstraint("clicks >= 0", name="ck_gsc_page_metric_clicks"),
        CheckConstraint("impressions >= 0", name="ck_gsc_page_metric_impressions"),
        CheckConstraint(
            "clicks <= impressions",
            name="ck_gsc_page_metric_clicks_impressions",
        ),
        CheckConstraint(
            "average_position_text IS NULL OR average_position_text <> ''",
            name="ck_gsc_page_metric_position",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    period_start: date = Field(sa_column=Column(Date, nullable=False, index=True))
    period_end: date = Field(sa_column=Column(Date, nullable=False, index=True))
    normalized_url: str = Field(sa_column=Column(Text, nullable=False))
    clicks: int
    impressions: int
    average_position_text: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    last_import_id: int = Field(foreign_key="gscimport.id", index=True)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(UTCDateTime(), nullable=False),
    )

    @property
    def average_position(self) -> Decimal | None:
        if self.average_position_text is None:
            return None
        return Decimal(self.average_position_text)

    @property
    def ctr(self) -> Decimal:
        if self.impressions == 0:
            return Decimal(0)
        return Decimal(self.clicks) / Decimal(self.impressions)


INTEGRATION_PROVIDERS = ("google_search_console", "yandex_webmaster")


class IntegrationConnection(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("site_id", "provider", name="uq_integration_connection_site_provider"),
        CheckConstraint("provider IN ('google_search_console','yandex_webmaster')", name="ck_integration_connection_provider"),
        CheckConstraint("status IN ('connecting','connected','reauthorization_required','error','disconnected')", name="ck_integration_connection_status"),
    )
    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    provider: str = Field(index=True)
    status: str = Field(default="connecting", index=True)
    access_token_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    refresh_token_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    token_expires_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime(), nullable=True))
    provider_user_id: str | None = None
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_column=Column(UTCDateTime(), nullable=False))


class IntegrationSource(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("connection_id", "version", name="uq_integration_source_version"), CheckConstraint("provider IN ('google_search_console','yandex_webmaster')", name="ck_integration_source_provider"), CheckConstraint("version >= 1", name="ck_integration_source_version"))
    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="integrationconnection.id", index=True)
    provider: str = Field(index=True)
    version: int
    resource_id: str = Field(sa_column=Column(Text, nullable=False))
    resource_label: str = Field(sa_column=Column(Text, nullable=False))
    active: bool = Field(default=True, sa_column=Column(Boolean, nullable=False, index=True))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_column=Column(UTCDateTime(), nullable=False))


class IntegrationOAuthAttempt(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("state_hash", name="uq_integration_oauth_state"), CheckConstraint("provider IN ('google_search_console','yandex_webmaster')", name="ck_integration_oauth_provider"), CheckConstraint("action IN ('connect','another')", name="ck_integration_oauth_action"))
    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    provider: str = Field(index=True)
    action: str
    state_hash: str = Field(index=True)
    pkce_verifier_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(sa_column=Column(UTCDateTime(), nullable=False))
    expires_at: datetime = Field(sa_column=Column(UTCDateTime(), nullable=False, index=True))
    used_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime(), nullable=True))


class IntegrationSchedule(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("connection_id", name="uq_integration_schedule_connection"), CheckConstraint("frequency IN ('daily','weekly')", name="ck_integration_schedule_frequency"), CheckConstraint("local_weekday >= 0 AND local_weekday <= 6", name="ck_integration_schedule_weekday"))
    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="integrationconnection.id", index=True)
    enabled: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    frequency: str = "weekly"
    local_weekday: int = 0
    local_time: str = "09:00"
    next_run_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime(), nullable=True, index=True))


class IntegrationSyncRun(SQLModel, table=True):
    __table_args__ = (CheckConstraint("status IN ('pending','running','completed','partial','failed','interrupted','cancelled')", name="ck_integration_sync_status"), CheckConstraint("trigger IN ('initial','manual','scheduled')", name="ck_integration_sync_trigger"), CheckConstraint("requested_start IS NULL OR requested_end IS NULL OR requested_start <= requested_end", name="ck_integration_sync_requested_period"), CheckConstraint("actual_start IS NULL OR actual_end IS NULL OR actual_start <= actual_end", name="ck_integration_sync_actual_period"), CheckConstraint("added_count >= 0 AND updated_count >= 0 AND unchanged_count >= 0 AND rejected_count >= 0", name="ck_integration_sync_counts"))
    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="integrationconnection.id", index=True)
    source_id: int | None = Field(default=None, foreign_key="integrationsource.id", index=True)
    trigger: str
    requested_start: date | None = None
    requested_end: date | None = None
    actual_start: date | None = None
    actual_end: date | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_column=Column(UTCDateTime(), nullable=False, index=True))
    started_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime(), nullable=True))
    completed_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime(), nullable=True))
    status: str = Field(default="pending", index=True)
    added_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    rejected_count: int = 0
    message: str = "Ожидает запуска."
    error_code: str | None = None


class IntegrationPageMetric(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("source_id", "metric_date", "normalized_url", name="uq_integration_metric_source_date_url"), CheckConstraint("provider IN ('google_search_console','yandex_webmaster')", name="ck_integration_metric_provider"), CheckConstraint("clicks IS NULL OR clicks >= 0", name="ck_integration_metric_clicks"), CheckConstraint("impressions IS NULL OR impressions >= 0", name="ck_integration_metric_impressions"), CheckConstraint("clicks IS NULL OR impressions IS NULL OR clicks <= impressions", name="ck_integration_metric_clicks_impressions"), CheckConstraint("position_text IS NULL OR position_text <> ''", name="ck_integration_metric_position"))
    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="integrationsource.id", index=True)
    provider: str = Field(index=True)
    metric_date: date = Field(sa_column=Column(Date, nullable=False, index=True))
    normalized_url: str = Field(sa_column=Column(Text, nullable=False))
    clicks: int | None = None
    impressions: int | None = None
    position_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_column=Column(UTCDateTime(), nullable=False))


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
    previous_run_id: int = Field(
        foreign_key="crawlrun.id",
        nullable=False,
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


class PriceChangeEvent(SQLModel, table=True):
    """Достоверное изменение цены между двумя completed-снимками страницы."""

    __table_args__ = (
        UniqueConstraint(
            "current_run_id",
            "previous_run_id",
            "url",
            name="uq_price_change_event_pair_url",
        ),
        CheckConstraint(
            "profile IN ('price', 'range')",
            name="ck_price_change_event_profile",
        ),
        CheckConstraint("currency <> ''", name="ck_price_change_event_currency"),
    )

    id: int | None = Field(default=None, primary_key=True)
    current_run_id: int = Field(foreign_key="crawlrun.id", index=True)
    previous_run_id: int = Field(foreign_key="crawlrun.id", index=True)
    current_page_record_id: int = Field(foreign_key="crawlpagerecord.id", index=True)
    previous_page_record_id: int = Field(foreign_key="crawlpagerecord.id", index=True)
    url: str = Field(index=True)
    current_completed_at: datetime = Field(
        sa_column=Column(UTCDateTime(), nullable=False, index=True),
    )
    profile: str
    currency: str


class ChangeEventViewState(SQLModel, table=True):
    """Локальное ручное состояние просмотра одного события."""

    __table_args__ = (
        UniqueConstraint(
            "snapshot_change_event_id",
            name="uq_change_event_view_state_snapshot_event",
        ),
        UniqueConstraint(
            "price_change_event_id",
            name="uq_change_event_view_state_price_event",
        ),
        CheckConstraint(
            "(snapshot_change_event_id IS NOT NULL AND price_change_event_id IS NULL) OR "
            "(snapshot_change_event_id IS NULL AND price_change_event_id IS NOT NULL)",
            name="ck_change_event_view_state_exactly_one_event",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    snapshot_change_event_id: int | None = Field(
        default=None,
        foreign_key="snapshotchangeevent.id",
    )
    price_change_event_id: int | None = Field(
        default=None,
        foreign_key="pricechangeevent.id",
    )
    viewed_at: datetime = Field(
        sa_column=Column(UTCDateTime(), nullable=False),
    )
