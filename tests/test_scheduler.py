import asyncio
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
import hashlib
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect
from sqlmodel import SQLModel
from sqlmodel import Session, select

from marketing_intelligence.config import SMTPConfig, Settings
from marketing_intelligence.crawl_dispatcher import CrawlQueueDispatcher
from marketing_intelligence.crawl_history import start_crawl_run
from marketing_intelligence.crawler import (
    CrawlCounters,
    CrawlPageResult,
    CrawlResult,
    CrawlSettings,
    CrawlStatus,
    PageOutcome,
)
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    CrawlRun,
    ScheduledCrawlEntry,
    Site,
    SiteSchedule,
)
from marketing_intelligence.page_content import extract_page_data
from marketing_intelligence.scheduler import (
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    MISSED,
    PENDING,
    calculate_next_run,
    create_retry,
    get_schedule,
    list_entries,
    load_schedule_summaries,
    parse_schedule_form,
    reconcile_missed_schedules,
    recover_interrupted_entries,
    reserve_due_entries,
    save_schedule,
)
from marketing_intelligence.sites import delete_site
from marketing_intelligence.smtp_notifications import SMTPNotifier, SMTPTransport


MOSCOW = ZoneInfo("Europe/Moscow")
FIXED_NOW = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)


def make_engine(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'scheduler.db').as_posix()}")
    initialize_database(engine)
    with Session(engine) as session:
        session.add(Site(name="Первый", url="https://example.com/"))
        session.commit()
    return engine


def valid_form(**overrides: str) -> dict[str, str]:
    form = {
        "enabled": "1",
        "frequency": "weekly",
        "local_weekday": "1",
        "local_time": "09:00",
        "max_pages": "200",
        "max_depth": "3",
        "delay": "1",
        "timeout": "15",
        "user_agent": "MarketingIntelligenceBot/0.1",
    }
    form.update(overrides)
    return form


def parsed_values(**overrides: str) -> dict[str, object]:
    values, errors = parse_schedule_form(valid_form(**overrides))
    assert errors == {}
    assert values is not None
    return values


def test_daily_and_weekly_next_run_are_future_utc() -> None:
    daily = calculate_next_run(
        frequency="daily",
        local_weekday=0,
        local_time="09:00",
        now=FIXED_NOW,
        local_timezone=MOSCOW,
    )
    weekly = calculate_next_run(
        frequency="weekly",
        local_weekday=FIXED_NOW.astimezone(MOSCOW).weekday(),
        local_time="09:00",
        now=FIXED_NOW,
        local_timezone=MOSCOW,
    )
    assert daily == datetime(2026, 7, 22, 6, 0, tzinfo=UTC)
    assert weekly == datetime(2026, 7, 28, 6, 0, tzinfo=UTC)
    assert daily > FIXED_NOW and weekly > FIXED_NOW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frequency", "monthly"),
        ("local_weekday", "7"),
        ("local_time", "24:00"),
        ("max_pages", "0"),
        ("user_agent", "Bot\nInjected"),
    ],
)
def test_schedule_validation_preserves_all_input(field: str, value: str) -> None:
    form = valid_form(**{field: value, "delay": "1,25"})
    values, errors = parse_schedule_form(form)
    assert values is None
    assert field in errors
    assert form["delay"] == "1,25"


def test_save_schedule_keeps_one_row_and_cancels_old_pending(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    schedule = save_schedule(
        engine, 1, parsed_values(), now=FIXED_NOW, local_timezone=MOSCOW
    )
    assert schedule.next_run_at is not None and schedule.next_run_at > FIXED_NOW
    with Session(engine) as session:
        session.add(
            ScheduledCrawlEntry(
                site_id=1,
                schedule_id=schedule.id,
                scheduled_for=FIXED_NOW,
                status=PENDING,
                message="Ожидает",
                max_pages=200,
                max_depth=3,
                delay=1,
                timeout=15,
                user_agent="Bot",
            )
        )
        session.commit()
    updated = save_schedule(
        engine,
        1,
        parsed_values(enabled="", frequency="daily"),
        now=FIXED_NOW + timedelta(minutes=1),
        local_timezone=MOSCOW,
    )
    with Session(engine) as session:
        schedules = session.exec(select(SiteSchedule)).all()
        entry = session.exec(select(ScheduledCrawlEntry)).one()
    assert len(schedules) == 1
    assert updated.enabled is False and updated.next_run_at is None
    assert entry.status == CANCELLED


def test_missed_periods_are_summarized_without_catch_up(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    save_schedule(engine, 1, parsed_values(frequency="daily"), now=FIXED_NOW)
    with Session(engine) as session:
        schedule = session.exec(select(SiteSchedule)).one()
        schedule.next_run_at = FIXED_NOW - timedelta(days=3)
        session.add(schedule)
        session.commit()
    assert reconcile_missed_schedules(engine, now=FIXED_NOW) == 1
    with Session(engine) as session:
        entry = session.exec(select(ScheduledCrawlEntry)).one()
        schedule = session.exec(select(SiteSchedule)).one()
    assert entry.status == MISSED
    assert entry.missed_periods == 4
    assert entry.notification_status == "not_applicable"
    assert schedule.next_run_at > FIXED_NOW


def test_repeated_due_tick_has_no_duplicate(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    save_schedule(engine, 1, parsed_values(frequency="daily"), now=FIXED_NOW)
    with Session(engine) as session:
        schedule = session.exec(select(SiteSchedule)).one()
        schedule.next_run_at = FIXED_NOW
        session.add(schedule)
        session.commit()
    assert reserve_due_entries(engine, now=FIXED_NOW) == 1
    assert reserve_due_entries(engine, now=FIXED_NOW) == 0
    assert list_entries(engine, 1, 1).total == 1


def test_restart_marks_pending_and_running_interrupted(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        for status in (PENDING, "running", COMPLETED):
            session.add(
                ScheduledCrawlEntry(
                    site_id=1,
                    scheduled_for=FIXED_NOW,
                    status=status,
                    message=status,
                    max_pages=1,
                    max_depth=0,
                    delay=1,
                    timeout=2,
                    user_agent="Bot",
                )
            )
        session.commit()
    assert recover_interrupted_entries(engine, now=FIXED_NOW) == 2
    with Session(engine) as session:
        entries = session.exec(select(ScheduledCrawlEntry).order_by(ScheduledCrawlEntry.id)).all()
    assert [entry.status for entry in entries] == [INTERRUPTED, INTERRUPTED, COMPLETED]
    assert entries[0].notification_status == "pending"


@pytest.mark.parametrize(
    "status", ["partial", "deferred", FAILED, INTERRUPTED]
)
def test_retry_uses_snapshot_only_for_allowed_statuses(tmp_path: Path, status: str) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        source = ScheduledCrawlEntry(
            site_id=1,
            scheduled_for=FIXED_NOW,
            status=status,
            message="Итог",
            max_pages=17,
            max_depth=4,
            delay=1.5,
            timeout=22,
            user_agent="SnapshotBot",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
    retry = create_retry(engine, 1, source.id, now=FIXED_NOW + timedelta(minutes=1))
    assert retry.status == PENDING and retry.retry_of_id == source.id
    assert (retry.max_pages, retry.user_agent) == (17, "SnapshotBot")
    assert get_schedule(engine, 1) is None


@pytest.mark.parametrize(
    "status", [COMPLETED, PENDING, "running", MISSED, CANCELLED]
)
def test_retry_rejects_forbidden_statuses(tmp_path: Path, status: str) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        entry = ScheduledCrawlEntry(
            site_id=1,
            scheduled_for=FIXED_NOW,
            status=status,
            message=status,
            max_pages=1,
            max_depth=0,
            delay=1,
            timeout=2,
            user_agent="Bot",
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
    with pytest.raises(ValueError):
        create_retry(engine, 1, entry.id, now=FIXED_NOW)


class OrderedCrawler:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def crawl(self, start_url, settings, *, progress=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.urls.append(start_url)
        await asyncio.sleep(0)
        page = CrawlPageResult(
            start_url,
            0,
            PageOutcome.HTML,
            "Страница обработана.",
            200,
            page_data=extract_page_data("<h1>Тест</h1>", ()),
        )
        self.active -= 1
        return CrawlResult(
            CrawlStatus.COMPLETED,
            "Обход завершён.",
            404,
            (page,),
            CrawlCounters(processed=1, requested=1, successful=1),
            False,
        )


def test_dispatcher_runs_two_sites_in_stable_sequence(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        session.add(Site(name="Второй", url="https://example.org/"))
        session.commit()
    for site_id in (2, 1):
        save_schedule(engine, site_id, parsed_values(frequency="daily"), now=FIXED_NOW)
    with Session(engine) as session:
        schedules = session.exec(select(SiteSchedule)).all()
        for schedule in schedules:
            schedule.next_run_at = FIXED_NOW
            session.add(schedule)
        session.commit()
    crawler = OrderedCrawler()
    notifier = SMTPNotifier(SMTPConfig())
    dispatcher = CrawlQueueDispatcher(engine, crawler, notifier, logging.getLogger("test"))

    async def run() -> None:
        await dispatcher.wake(now=FIXED_NOW)
        await dispatcher.wait_idle()

    asyncio.run(run())
    assert crawler.urls == ["https://example.com/", "https://example.org/"]
    assert crawler.max_active == 1
    with Session(engine) as session:
        entries = session.exec(select(ScheduledCrawlEntry).order_by(ScheduledCrawlEntry.id)).all()
        runs = session.exec(select(CrawlRun).order_by(CrawlRun.id)).all()
    assert [entry.status for entry in entries] == [COMPLETED, COMPLETED]
    assert all(entry.crawl_run_id is not None for entry in entries)
    assert len(runs) == 2


def test_manual_crawl_conflict_leaves_queue_pending(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    manual = start_crawl_run(engine, 1, CrawlSettings())
    with Session(engine) as session:
        session.add(
            ScheduledCrawlEntry(
                site_id=1,
                scheduled_for=FIXED_NOW,
                status=PENDING,
                message="Ожидает",
                max_pages=1,
                max_depth=0,
                delay=1,
                timeout=2,
                user_agent="Bot",
            )
        )
        session.commit()
    crawler = OrderedCrawler()
    dispatcher = CrawlQueueDispatcher(
        engine, crawler, SMTPNotifier(SMTPConfig()), logging.getLogger("test")
    )

    async def run() -> None:
        await dispatcher.wake(now=FIXED_NOW)
        await dispatcher.wait_idle()

    asyncio.run(run())
    with Session(engine) as session:
        entry = session.exec(select(ScheduledCrawlEntry)).one()
    assert manual.id is not None
    assert entry.status == PENDING and entry.crawl_run_id is None
    assert crawler.urls == []


class RecordingTransport:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[EmailMessage] = []

    def send(self, config: SMTPConfig, message: EmailMessage) -> None:
        if self.fail:
            raise OSError("SMTP unavailable")
        self.messages.append(message)


def smtp_config(**changes) -> SMTPConfig:
    values = {
        "host": "smtp.example",
        "security": "starttls",
        "port": 587,
        "from_address": "from@example.com",
        "to_address": "to@example.com",
    }
    values.update(changes)
    return SMTPConfig(**values)


def test_optional_smtp_and_failure_are_separate_from_result() -> None:
    site = Site(id=1, name="Сайт", url="https://example.com")
    entry = ScheduledCrawlEntry(
        id=1,
        site_id=1,
        scheduled_for=FIXED_NOW,
        status=FAILED,
        message="Ошибка обхода",
        max_pages=1,
        max_depth=0,
        delay=1,
        timeout=2,
        user_agent="Bot",
    )
    disabled = asyncio.run(SMTPNotifier(SMTPConfig()).send_entry(entry, site))
    failed = asyncio.run(
        SMTPNotifier(smtp_config(), RecordingTransport(fail=True)).send_entry(entry, site)
    )
    transport = RecordingTransport()
    sent = asyncio.run(SMTPNotifier(smtp_config(), transport).send_entry(entry, site))
    assert (disabled, failed, sent) == ("disabled", "failed", "sent")
    assert transport.messages[0].get_content_type() == "text/plain"
    assert "Ошибка обхода" in transport.messages[0].get_content()


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_standard_smtp_transport_uses_encryption(monkeypatch, security: str) -> None:
    events: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            events.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, **kwargs):
            events.append("starttls")

        def send_message(self, message):
            events.append("send")

    monkeypatch.setattr("marketing_intelligence.smtp_notifications.smtplib.SMTP", Client)
    monkeypatch.setattr("marketing_intelligence.smtp_notifications.smtplib.SMTP_SSL", Client)
    message = EmailMessage()
    message.set_content("test")
    port = 465 if security == "ssl" else 587
    SMTPTransport().send(smtp_config(security=security, port=port), message)
    assert events[-1] == "send"
    assert ("starttls" in events) is (security == "starttls")


def test_invalid_smtp_environment_is_visible_without_secret(monkeypatch) -> None:
    monkeypatch.setenv("MI_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("MI_SMTP_FROM", "from@example.com")
    monkeypatch.setenv("MI_SMTP_TO", "to@example.com")
    monkeypatch.setenv("MI_SMTP_USERNAME", "user")
    monkeypatch.setenv("MI_SMTP_PASSWORD", "top-secret")
    monkeypatch.setenv("MI_SMTP_TIMEOUT", "99")
    config = SMTPConfig.from_environment()
    assert config.enabled is False
    assert "MI_SMTP_TIMEOUT" in config.error
    assert "top-secret" not in repr(config)
    assert "top-secret" not in (config.error or "")


def build_app(tmp_path: Path, *, smtp: SMTPConfig | None = None, transport=None):
    database_path = tmp_path / "data" / "app.db"
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database_path.as_posix()}",
        local_timezone=MOSCOW,
        smtp=smtp or SMTPConfig(),
    )
    return (
        create_app(
            settings,
            smtp_transport=transport,
            now_provider=lambda: FIXED_NOW,
        ),
        database_path,
    )


def schedule_token(client: TestClient) -> str:
    response = client.get("/sites/1/schedule")
    match = re.search(
        r'action="/sites/1/schedule" method="post".*?name="action_token" value="([^"]+)"',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_schedule_ui_validation_future_save_and_clean_get(tmp_path: Path) -> None:
    app, database_path = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/sites", data={"name": "Сайт", "url": "https://example.com"})
        screen = client.get("/sites/1/schedule")
        assert screen.status_code == 200
        assert "этапу 14" in screen.text
        assert "Расписание выключено" in screen.text
        assert '<details class="advanced-settings">' in screen.text
        assert '<option value="1" selected>Вторник</option>' in screen.text
        assert client.get("/sites/1/schedule?page=0").status_code == 422
        before = hashlib.sha256(database_path.read_bytes()).hexdigest()
        assert client.get("/sites/1/schedule").status_code == 200
        after = hashlib.sha256(database_path.read_bytes()).hexdigest()
        assert after == before

        invalid = client.post(
            "/sites/1/schedule",
            data={**valid_form(local_time="99:00"), "action_token": schedule_token(client)},
        )
        assert invalid.status_code == 422
        assert 'value="99:00"' in invalid.text
        assert '<details class="advanced-settings" open>' in invalid.text
        assert "Введите время в формате" in invalid.text

        saved = client.post(
            "/sites/1/schedule",
            data={**valid_form(), "action_token": schedule_token(client)},
            follow_redirects=False,
        )
        assert saved.status_code == 303
    schedule = get_schedule(app.state.engine, 1)
    assert schedule is not None and schedule.enabled
    assert schedule.next_run_at > FIXED_NOW


def test_test_email_post_and_password_absent_from_html(tmp_path: Path) -> None:
    transport = RecordingTransport()
    app, _ = build_app(
        tmp_path,
        smtp=smtp_config(password="top-secret", username="user"),
        transport=transport,
    )
    with TestClient(app) as client:
        client.post("/sites", data={"name": "Сайт", "url": "https://example.com"})
        screen = client.get("/sites/1/schedule")
        assert "top-secret" not in screen.text
        match = re.search(
            r'action="/sites/1/schedule/test-email" method="post">\s*<input type="hidden" name="action_token" value="([^"]+)"',
            screen.text,
        )
        assert match is not None
        response = client.post(
            "/sites/1/schedule/test-email",
            data={"action_token": match.group(1)},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"].endswith("email_test=sent")
    assert len(transport.messages) == 1


def test_schedule_rows_delete_with_site_and_other_site_survives(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        session.add(Site(name="Второй", url="https://example.org"))
        session.commit()
    for site_id in (1, 2):
        schedule = save_schedule(engine, site_id, parsed_values(), now=FIXED_NOW)
        with Session(engine) as session:
            session.add(
                ScheduledCrawlEntry(
                    site_id=site_id,
                    schedule_id=schedule.id,
                    scheduled_for=FIXED_NOW,
                    status=MISSED,
                    message="Пропущен",
                    max_pages=1,
                    max_depth=0,
                    delay=1,
                    timeout=2,
                    user_agent="Bot",
                )
            )
            session.commit()
    assert delete_site(engine, 1)
    with Session(engine) as session:
        assert session.get(Site, 1) is None
        assert session.get(Site, 2) is not None
        assert [row.site_id for row in session.exec(select(SiteSchedule)).all()] == [2]
        assert [row.site_id for row in session.exec(select(ScheduledCrawlEntry)).all()] == [2]


def test_journal_pagination_is_twenty_and_validated(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        for number in range(21):
            session.add(
                ScheduledCrawlEntry(
                    site_id=1,
                    scheduled_for=FIXED_NOW + timedelta(minutes=number),
                    status=MISSED,
                    message=str(number),
                    max_pages=1,
                    max_depth=0,
                    delay=1,
                    timeout=2,
                    user_agent="Bot",
                )
            )
        session.commit()
    first = list_entries(engine, 1, 1)
    second = list_entries(engine, 1, 2)
    assert len(first.entries) == 20 and len(second.entries) == 1
    assert first.entries[0].message == "20"
    with pytest.raises(ValueError):
        list_entries(engine, 1, 0)


def test_old_sqlite_adds_only_scheduler_tables_and_keeps_data(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    new_names = {"siteschedule", "scheduledcrawlentry"}
    old_tables = [
        table for table in SQLModel.metadata.sorted_tables if table.name not in new_names
    ]
    SQLModel.metadata.create_all(engine, tables=old_tables)
    with Session(engine) as session:
        session.add(Site(name="Сохранённый сайт", url="https://example.com"))
        session.commit()
    before = set(inspect(engine).get_table_names())
    initialize_database(engine)
    after = set(inspect(engine).get_table_names())
    with Session(engine) as session:
        site = session.exec(select(Site)).one()
    assert after - before == new_names
    assert site.name == "Сохранённый сайт"


def test_home_schedule_summary_query_count_does_not_grow_with_sites(
    tmp_path: Path,
) -> None:
    engine = make_engine(tmp_path)
    with Session(engine) as session:
        session.add(Site(name="Второй", url="https://example.org"))
        session.add(Site(name="Третий", url="https://example.net"))
        session.commit()
    for site_id in (1, 2, 3):
        save_schedule(engine, site_id, parsed_values(), now=FIXED_NOW)
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        summaries = load_schedule_summaries(engine, [1, 2, 3])
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert set(summaries) == {1, 2, 3}
    assert len(statements) == 2
