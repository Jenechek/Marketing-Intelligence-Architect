"""Сохраняемые расписания и последовательная очередь полных обходов."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
import re

from sqlalchemy import delete, func, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from tzlocal import get_localzone

from .crawl_settings import default_crawl_form, parse_crawl_settings
from .crawler import CrawlSettings
from .models import ScheduledCrawlEntry, Site, SiteSchedule


PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
PARTIAL = "partial"
DEFERRED = "deferred"
FAILED = "failed"
INTERRUPTED = "interrupted"
MISSED = "missed"
CANCELLED = "cancelled"
RETRYABLE = {PARTIAL, DEFERRED, FAILED, INTERRUPTED}
TERMINAL = {COMPLETED, PARTIAL, DEFERRED, FAILED, INTERRUPTED, MISSED, CANCELLED}
ENTRIES_PER_PAGE = 20

STATUS_TITLES = {
    PENDING: "Ожидает",
    RUNNING: "Выполняется",
    COMPLETED: "Завершён",
    PARTIAL: "Завершён частично",
    DEFERRED: "Отложен",
    FAILED: "Ошибка",
    INTERRUPTED: "Прерван",
    MISSED: "Пропущен",
    CANCELLED: "Отменён",
}
FREQUENCY_TITLES = {"daily": "Ежедневно", "weekly": "Еженедельно"}
WEEKDAY_TITLES = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)


@dataclass(frozen=True, slots=True)
class SchedulePage:
    entries: tuple[ScheduledCrawlEntry, ...]
    page: int
    page_count: int
    total: int


@dataclass(frozen=True, slots=True)
class SiteScheduleSummary:
    enabled: bool
    next_run_at: datetime | None
    last_status: str | None


def effective_timezone(value: tzinfo | None) -> tzinfo:
    """Вернуть явно настроенный или системный локальный часовой пояс."""

    return value or get_localzone()


def default_schedule_form(
    *,
    local_timezone: tzinfo | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Без записи в БД подготовить безопасные значения новой формы."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")
    local_now = current.astimezone(effective_timezone(local_timezone))
    return {
        "enabled": "",
        "frequency": "weekly",
        "local_weekday": str(local_now.weekday()),
        "local_time": "09:00",
        **default_crawl_form(),
    }


def schedule_to_form(schedule: SiteSchedule) -> dict[str, str]:
    return {
        "enabled": "1" if schedule.enabled else "",
        "frequency": schedule.frequency,
        "local_weekday": str(schedule.local_weekday),
        "local_time": schedule.local_time,
        "max_pages": str(schedule.max_pages),
        "max_depth": str(schedule.max_depth),
        "delay": _format_number(schedule.delay),
        "timeout": _format_number(schedule.timeout),
        "user_agent": schedule.user_agent,
    }


def parse_schedule_form(
    form: dict[str, str],
) -> tuple[dict[str, object] | None, dict[str, str]]:
    """Полностью проверить расписание и вложенные настройки crawler."""

    errors: dict[str, str] = {}
    frequency = form.get("frequency", "")
    if frequency not in FREQUENCY_TITLES:
        errors["frequency"] = "Выберите ежедневное или еженедельное расписание."

    weekday_text = form.get("local_weekday", "")
    if not weekday_text.isdigit() or not 0 <= int(weekday_text) <= 6:
        errors["local_weekday"] = "Выберите день недели."
        weekday = 0
    else:
        weekday = int(weekday_text)

    local_time = form.get("local_time", "")
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time) is None:
        errors["local_time"] = "Введите время в формате ЧЧ:ММ."

    crawl_form = {
        key: form.get(key, default)
        for key, default in default_crawl_form().items()
    }
    settings, crawl_errors = parse_crawl_settings(crawl_form)
    errors.update(crawl_errors)
    if errors:
        return None, errors
    assert settings is not None
    return {
        "enabled": form.get("enabled") == "1",
        "frequency": frequency,
        "local_weekday": weekday,
        "local_time": local_time,
        "settings": settings,
    }, {}


def calculate_next_run(
    *,
    frequency: str,
    local_weekday: int,
    local_time: str,
    now: datetime,
    local_timezone: tzinfo | None,
) -> datetime:
    """Рассчитать строго будущий момент и вернуть timezone-aware UTC."""

    if now.tzinfo is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")
    zone = effective_timezone(local_timezone)
    local_now = now.astimezone(zone)
    hour, minute = (int(part) for part in local_time.split(":"))
    candidate = datetime.combine(local_now.date(), time(hour, minute), zone)
    if frequency == "daily":
        if candidate <= local_now:
            candidate += timedelta(days=1)
    elif frequency == "weekly":
        candidate += timedelta(days=(local_weekday - local_now.weekday()) % 7)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    else:
        raise ValueError("Неизвестная частота расписания.")
    return candidate.astimezone(UTC)


def advance_run(
    value: datetime,
    *,
    frequency: str,
    local_timezone: tzinfo | None,
) -> datetime:
    """Продвинуть момент по локальному календарю с учётом смены UTC-смещения."""

    zone = effective_timezone(local_timezone)
    local_value = value.astimezone(zone)
    days = 1 if frequency == "daily" else 7
    return (local_value + timedelta(days=days)).astimezone(UTC)


def get_schedule(engine: Engine, site_id: int) -> SiteSchedule | None:
    with Session(engine) as session:
        return session.exec(
            select(SiteSchedule).where(SiteSchedule.site_id == site_id)
        ).first()


def save_schedule(
    engine: Engine,
    site_id: int,
    values: dict[str, object],
    *,
    now: datetime | None = None,
    local_timezone: tzinfo | None = None,
) -> SiteSchedule:
    """Сохранить правило и отменить прежние ещё не начавшиеся записи."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")
    settings = values["settings"]
    if not isinstance(settings, CrawlSettings):
        raise TypeError("Настройки crawler имеют неверный тип.")

    with Session(engine, expire_on_commit=False) as session:
        _begin_write(session, engine)
        if session.get(Site, site_id) is None:
            raise LookupError("Сайт для расписания не найден.")
        schedule = session.exec(
            select(SiteSchedule).where(SiteSchedule.site_id == site_id)
        ).first()
        if schedule is None:
            schedule = SiteSchedule(
                site_id=site_id,
                enabled=False,
                frequency="weekly",
                local_weekday=current.astimezone(
                    effective_timezone(local_timezone)
                ).weekday(),
                local_time="09:00",
                max_pages=settings.max_pages,
                max_depth=settings.max_depth,
                delay=settings.delay,
                timeout=settings.timeout,
                user_agent=settings.user_agent,
            )
            session.add(schedule)
            session.flush()
        if schedule.id is not None:
            session.exec(
                update(ScheduledCrawlEntry)
                .where(
                    ScheduledCrawlEntry.schedule_id == schedule.id,
                    ScheduledCrawlEntry.status == PENDING,
                )
                .values(
                    status=CANCELLED,
                    message="Запись отменена после изменения расписания.",
                    completed_at=current.astimezone(UTC),
                    notification_status="not_applicable",
                )
            )

        schedule.enabled = bool(values["enabled"])
        schedule.frequency = str(values["frequency"])
        schedule.local_weekday = int(values["local_weekday"])
        schedule.local_time = str(values["local_time"])
        schedule.max_pages = settings.max_pages
        schedule.max_depth = settings.max_depth
        schedule.delay = settings.delay
        schedule.timeout = settings.timeout
        schedule.user_agent = settings.user_agent
        schedule.updated_at = current.astimezone(UTC)
        schedule.next_run_at = (
            calculate_next_run(
                frequency=schedule.frequency,
                local_weekday=schedule.local_weekday,
                local_time=schedule.local_time,
                now=current,
                local_timezone=local_timezone,
            )
            if schedule.enabled
            else None
        )
        session.add(schedule)
        session.commit()
        return schedule


def recover_interrupted_entries(engine: Engine, *, now: datetime | None = None) -> int:
    """Не продолжать очередь прошлого процесса автоматически."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    with Session(engine) as session:
        result = session.exec(
            update(ScheduledCrawlEntry)
            .where(ScheduledCrawlEntry.status.in_((PENDING, RUNNING)))
            .values(
                status=INTERRUPTED,
                message="Запуск был прерван остановкой приложения.",
                completed_at=current,
                notification_status="pending",
            )
        )
        session.commit()
        return result.rowcount or 0


def reconcile_missed_schedules(
    engine: Engine,
    *,
    now: datetime | None = None,
    local_timezone: tzinfo | None = None,
) -> int:
    """Свести пропущенные периоды без запуска задним числом и без письма."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    created = 0
    with Session(engine) as session:
        _begin_write(session, engine)
        schedules = list(
            session.exec(
                select(SiteSchedule)
                .where(
                    SiteSchedule.enabled.is_(True),
                    SiteSchedule.next_run_at <= current,
                )
                .order_by(SiteSchedule.next_run_at, SiteSchedule.site_id)
            ).all()
        )
        for schedule in schedules:
            assert schedule.next_run_at is not None
            first_missed = schedule.next_run_at
            missed_periods = 0
            while schedule.next_run_at <= current:
                missed_periods += 1
                schedule.next_run_at = advance_run(
                    schedule.next_run_at,
                    frequency=schedule.frequency,
                    local_timezone=local_timezone,
                )
            session.add(
                _entry_from_schedule(
                    schedule,
                    scheduled_for=first_missed,
                    status=MISSED,
                    message=f"Пропущено периодов: {missed_periods}.",
                    completed_at=current,
                    missed_periods=missed_periods,
                    notification_status="not_applicable",
                )
            )
            session.add(schedule)
            created += 1
        session.commit()
    return created


def reserve_due_entries(
    engine: Engine,
    *,
    now: datetime | None = None,
    local_timezone: tzinfo | None = None,
) -> int:
    """Транзакционно зарезервировать каждый наступивший плановый момент один раз."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    created = 0
    with Session(engine) as session:
        _begin_write(session, engine)
        schedules = list(
            session.exec(
                select(SiteSchedule)
                .where(
                    SiteSchedule.enabled.is_(True),
                    SiteSchedule.next_run_at <= current,
                )
                .order_by(SiteSchedule.next_run_at, SiteSchedule.site_id)
            ).all()
        )
        for schedule in schedules:
            assert schedule.next_run_at is not None
            moment = schedule.next_run_at
            session.add(
                _entry_from_schedule(
                    schedule,
                    scheduled_for=moment,
                    status=PENDING,
                    message="Запуск ожидает освобождения очереди.",
                )
            )
            schedule.next_run_at = advance_run(
                moment,
                frequency=schedule.frequency,
                local_timezone=local_timezone,
            )
            session.add(schedule)
            created += 1
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return 0
    return created


def next_pending_entry(engine: Engine) -> ScheduledCrawlEntry | None:
    with Session(engine) as session:
        return session.exec(
            select(ScheduledCrawlEntry)
            .where(ScheduledCrawlEntry.status == PENDING)
            .order_by(
                ScheduledCrawlEntry.scheduled_for,
                ScheduledCrawlEntry.site_id,
                ScheduledCrawlEntry.id,
            )
        ).first()


def mark_entry_running(
    engine: Engine,
    entry_id: int,
    crawl_run_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with Session(engine) as session:
        result = session.exec(
            update(ScheduledCrawlEntry)
            .where(
                ScheduledCrawlEntry.id == entry_id,
                ScheduledCrawlEntry.status == RUNNING,
                ScheduledCrawlEntry.crawl_run_id.is_(None),
            )
            .values(
                status=RUNNING,
                message="Полный обход выполняется.",
                started_at=current,
                crawl_run_id=crawl_run_id,
            )
        )
        session.commit()
        return bool(result.rowcount)


def claim_entry(
    engine: Engine,
    entry_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Атомарно забрать ожидающую запись одним локальным диспетчером."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    with Session(engine) as session:
        result = session.exec(
            update(ScheduledCrawlEntry)
            .where(
                ScheduledCrawlEntry.id == entry_id,
                ScheduledCrawlEntry.status == PENDING,
            )
            .values(
                status=RUNNING,
                message="Запись передана локальному диспетчеру.",
                started_at=current,
            )
        )
        session.commit()
        return bool(result.rowcount)


def release_entry(engine: Engine, entry_id: int) -> None:
    """Вернуть запись в ожидание, если обычный ручной обход занял crawler."""

    with Session(engine) as session:
        session.exec(
            update(ScheduledCrawlEntry)
            .where(
                ScheduledCrawlEntry.id == entry_id,
                ScheduledCrawlEntry.status == RUNNING,
                ScheduledCrawlEntry.crawl_run_id.is_(None),
            )
            .values(
                status=PENDING,
                message="Запуск ожидает завершения другого полного обхода.",
                started_at=None,
            )
        )
        session.commit()


def complete_entry(
    engine: Engine,
    entry_id: int,
    *,
    status: str,
    message: str,
    notification_status: str,
    now: datetime | None = None,
) -> None:
    if status not in TERMINAL:
        raise ValueError("Нельзя завершить запись нетерминальным статусом.")
    with Session(engine) as session:
        entry = session.get(ScheduledCrawlEntry, entry_id)
        if entry is None:
            raise LookupError("Запись журнала не найдена.")
        entry.status = status
        entry.message = message
        entry.completed_at = (now or datetime.now(UTC)).astimezone(UTC)
        entry.notification_status = notification_status
        session.add(entry)
        session.commit()


def set_notification_status(engine: Engine, entry_id: int, status: str) -> None:
    with Session(engine) as session:
        entry = session.get(ScheduledCrawlEntry, entry_id)
        if entry is None:
            return
        entry.notification_status = status
        session.add(entry)
        session.commit()


def create_retry(
    engine: Engine,
    site_id: int,
    entry_id: int,
    *,
    now: datetime | None = None,
) -> ScheduledCrawlEntry:
    """Создать новую очередь по неизменному снимку допустимой исходной записи."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    with Session(engine, expire_on_commit=False) as session:
        _begin_write(session, engine)
        source = session.get(ScheduledCrawlEntry, entry_id)
        if source is None or source.site_id != site_id:
            raise LookupError("Запись журнала для этого сайта не найдена.")
        if source.status not in RETRYABLE:
            raise ValueError("Запись с этим статусом нельзя повторить.")
        retry = ScheduledCrawlEntry(
            site_id=site_id,
            schedule_id=None,
            scheduled_for=current,
            status=PENDING,
            message="Ручной повтор ожидает освобождения очереди.",
            max_pages=source.max_pages,
            max_depth=source.max_depth,
            delay=source.delay,
            timeout=source.timeout,
            user_agent=source.user_agent,
            retry_of_id=source.id,
            notification_status="not_applicable",
        )
        session.add(retry)
        session.commit()
        return retry


def get_entry(engine: Engine, entry_id: int) -> ScheduledCrawlEntry | None:
    with Session(engine) as session:
        return session.get(ScheduledCrawlEntry, entry_id)


def list_pending_notifications(engine: Engine) -> list[ScheduledCrawlEntry]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(ScheduledCrawlEntry)
                .where(ScheduledCrawlEntry.notification_status == "pending")
                .order_by(ScheduledCrawlEntry.id)
            ).all()
        )


def list_entries(engine: Engine, site_id: int, page: int) -> SchedulePage:
    """Вернуть стабильную серверную страницу журнала по 20 записей."""

    if page < 1 or page > 1_000_000:
        raise ValueError("Номер страницы должен быть положительным целым числом.")
    with Session(engine) as session:
        total = session.exec(
            select(func.count())
            .select_from(ScheduledCrawlEntry)
            .where(ScheduledCrawlEntry.site_id == site_id)
        ).one()
        page_count = max(1, (total + ENTRIES_PER_PAGE - 1) // ENTRIES_PER_PAGE)
        if page > page_count:
            page = page_count
        entries = tuple(
            session.exec(
                select(ScheduledCrawlEntry)
                .where(ScheduledCrawlEntry.site_id == site_id)
                .order_by(
                    ScheduledCrawlEntry.scheduled_for.desc(),
                    ScheduledCrawlEntry.id.desc(),
                )
                .offset((page - 1) * ENTRIES_PER_PAGE)
                .limit(ENTRIES_PER_PAGE)
            ).all()
        )
    return SchedulePage(entries, page, page_count, total)


def count_schedule_data(engine: Engine, site_id: int) -> tuple[int, int]:
    with Session(engine) as session:
        schedules = session.exec(
            select(func.count())
            .select_from(SiteSchedule)
            .where(SiteSchedule.site_id == site_id)
        ).one()
        entries = session.exec(
            select(func.count())
            .select_from(ScheduledCrawlEntry)
            .where(ScheduledCrawlEntry.site_id == site_id)
        ).one()
        return schedules, entries


def load_schedule_summaries(
    engine: Engine,
    site_ids: list[int],
) -> dict[int, SiteScheduleSummary]:
    """Пакетно загрузить расписания и последние автоматические итоги без N+1."""

    if not site_ids:
        return {}
    with Session(engine) as session:
        schedules = list(
            session.exec(
                select(SiteSchedule).where(SiteSchedule.site_id.in_(site_ids))
            ).all()
        )
        ranked = (
            select(
                ScheduledCrawlEntry.site_id.label("site_id"),
                ScheduledCrawlEntry.status.label("status"),
                func.row_number()
                .over(
                    partition_by=ScheduledCrawlEntry.site_id,
                    order_by=(
                        ScheduledCrawlEntry.scheduled_for.desc(),
                        ScheduledCrawlEntry.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(
                ScheduledCrawlEntry.site_id.in_(site_ids),
                ScheduledCrawlEntry.schedule_id.is_not(None),
                ScheduledCrawlEntry.status.in_(TERMINAL),
            )
            .subquery()
        )
        latest = dict(
            session.exec(
                select(ranked.c.site_id, ranked.c.status).where(
                    ranked.c.position == 1
                )
            ).all()
        )
    by_site = {schedule.site_id: schedule for schedule in schedules}
    return {
        site_id: SiteScheduleSummary(
            enabled=bool(by_site.get(site_id) and by_site[site_id].enabled),
            next_run_at=by_site[site_id].next_run_at if site_id in by_site else None,
            last_status=latest.get(site_id),
        )
        for site_id in site_ids
    }


def delete_schedule_data(session: Session, site_id: int) -> None:
    """Удалить обе новые связи внутри внешней транзакции удаления сайта."""

    session.exec(
        delete(ScheduledCrawlEntry).where(ScheduledCrawlEntry.site_id == site_id)
    )
    session.exec(delete(SiteSchedule).where(SiteSchedule.site_id == site_id))


def entry_settings(entry: ScheduledCrawlEntry) -> CrawlSettings:
    return CrawlSettings(
        max_pages=entry.max_pages,
        max_depth=entry.max_depth,
        delay=entry.delay,
        timeout=entry.timeout,
        user_agent=entry.user_agent,
    )


def status_title(status: str) -> str:
    return STATUS_TITLES.get(status, "Неизвестный статус")


def notification_status_title(status: str) -> str:
    return {
        "not_applicable": "не требуется",
        "disabled": "выключено",
        "pending": "ожидает отправки",
        "sent": "отправлено",
        "failed": "ошибка отправки",
    }.get(status, "неизвестно")


def _entry_from_schedule(
    schedule: SiteSchedule,
    *,
    scheduled_for: datetime,
    status: str,
    message: str,
    completed_at: datetime | None = None,
    missed_periods: int = 0,
    notification_status: str = "not_applicable",
) -> ScheduledCrawlEntry:
    if schedule.id is None:
        raise LookupError("Расписание не имеет идентификатора.")
    return ScheduledCrawlEntry(
        site_id=schedule.site_id,
        schedule_id=schedule.id,
        scheduled_for=scheduled_for,
        completed_at=completed_at,
        status=status,
        message=message,
        max_pages=schedule.max_pages,
        max_depth=schedule.max_depth,
        delay=schedule.delay,
        timeout=schedule.timeout,
        user_agent=schedule.user_agent,
        missed_periods=missed_periods,
        notification_status=notification_status,
    )


def _begin_write(session: Session, engine: Engine) -> None:
    if engine.dialect.name == "sqlite":
        session.exec(text("BEGIN IMMEDIATE"))


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
