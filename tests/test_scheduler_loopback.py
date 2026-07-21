from datetime import UTC, datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from marketing_intelligence.config import SMTPConfig, Settings
from marketing_intelligence.crawler import Crawler, CrawlSettings
from marketing_intelligence.database import build_engine
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    CrawlPageSnapshot,
    ScheduledCrawlEntry,
    SiteSchedule,
    SnapshotChangeEvent,
)
from marketing_intelligence.scheduler import MISSED, PARTIAL, get_schedule, save_schedule


class RecordingTransport:
    def __init__(self) -> None:
        self.messages = []

    def send(self, config, message) -> None:
        self.messages.append(message)


class TrackingCrawler:
    def __init__(self) -> None:
        self.actual = Crawler()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    async def crawl(self, start_url, settings, *, progress=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append(start_url)
        try:
            return await self.actual.crawl(start_url, settings, progress=progress)
        finally:
            self.active -= 1


def wait_for_entries(app, count: int, *, timeout: float = 12) -> list[ScheduledCrawlEntry]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(app.state.engine) as session:
            entries = list(
                session.exec(
                    select(ScheduledCrawlEntry).order_by(ScheduledCrawlEntry.id)
                ).all()
            )
        if len(entries) >= count and all(
            entry.status not in {"pending", "running"} for entry in entries
        ) and all(entry.notification_status != "pending" for entry in entries):
            return entries
        time.sleep(0.05)
    raise AssertionError("Очередь loopback-сценария не завершилась вовремя.")


def set_due(engine, site_ids: tuple[int, ...], moment: datetime) -> None:
    with Session(engine) as session:
        schedules = session.exec(
            select(SiteSchedule).where(SiteSchedule.site_id.in_(site_ids))
        ).all()
        for schedule in schedules:
            schedule.next_run_at = moment
            session.add(schedule)
        session.commit()


def test_real_scheduler_loopback_end_to_end(tmp_path: Path, monkeypatch) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    state = {"version": 1}
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
                return
            if self.path == "/site2/bad":
                self.send_response(503)
                self.end_headers()
                return
            if self.path == "/site1":
                body = (
                    f'<html><title>Версия {state["version"]}</title>'
                    '<body><a href="/site1/a">A</a></body></html>'
                ).encode()
            elif self.path == "/site1/a":
                body = f'<html><body>Текст {state["version"]}</body></html>'.encode()
            else:
                body = b'<html><body><a href="/site2/bad">bad</a></body></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    database_path = tmp_path / "data" / "loopback.db"
    clock = [datetime(2026, 7, 21, 9, 0, tzinfo=UTC)]
    smtp = SMTPConfig(
        host="smtp.loopback",
        security="starttls",
        port=587,
        from_address="from@example.com",
        to_address="to@example.com",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database_path.as_posix()}",
        smtp=smtp,
    )
    crawler = TrackingCrawler()
    transport = RecordingTransport()
    app = create_app(
        settings,
        crawler=crawler,
        smtp_transport=transport,
        now_provider=lambda: clock[0],
    )
    try:
        with TestClient(app) as client:
            for name, path in (("Первый", "/site1"), ("Второй", "/site2")):
                response = client.post(
                    "/sites",
                    data={
                        "name": name,
                        "url": f"http://127.0.0.1:{server.server_port}{path}",
                    },
                    follow_redirects=False,
                )
                assert response.status_code == 303
            values = {
                "enabled": True,
                "frequency": "daily",
                "local_weekday": 1,
                "local_time": "12:00",
                "settings": CrawlSettings(
                    max_pages=2,
                    max_depth=1,
                    delay=0.5,
                    timeout=3,
                    user_agent="LoopbackSchedulerBot/1.0",
                ),
            }
            save_schedule(app.state.engine, 1, values, now=clock[0])
            save_schedule(app.state.engine, 2, values, now=clock[0])
            set_due(app.state.engine, (1, 2), clock[0])

            # Ни одного браузерного запроса во время ожидания: работает процесс FastAPI.
            entries = wait_for_entries(app, 2)
            assert [entry.status for entry in entries] == ["completed", PARTIAL]
            assert crawler.max_active == 1
            assert crawler.calls == [
                f"http://127.0.0.1:{server.server_port}/site1",
                f"http://127.0.0.1:{server.server_port}/site2",
            ]
            assert len(transport.messages) == 1

            state["version"] = 2
            clock[0] += timedelta(minutes=1)
            set_due(app.state.engine, (1,), clock[0])
            entries = wait_for_entries(app, 3)
            assert entries[-1].status == "completed"
            with Session(app.state.engine) as session:
                assert len(session.exec(select(CrawlPageSnapshot)).all()) == 5
                assert len(session.exec(select(SnapshotChangeEvent)).all()) >= 1

            schedule_page = client.get("/sites/2/schedule")
            retry_match = re.search(
                r'action="/sites/2/schedule/(\d+)/retry" method="post">\s*'
                r'<input type="hidden" name="action_token" value="([^"]+)"',
                schedule_page.text,
            )
            assert retry_match is not None
            retry = client.post(
                f"/sites/2/schedule/{retry_match.group(1)}/retry",
                data={"action_token": retry_match.group(2)},
                follow_redirects=False,
            )
            assert retry.status_code == 303
            entries = wait_for_entries(app, 4)
            assert entries[-1].status == PARTIAL
            assert entries[-1].retry_of_id is not None
            assert len(transport.messages) == 2
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    # Имитируем остановленное приложение: срок проходит без catch-up.
    app.state.engine.dispose()
    offline_due = clock[0] + timedelta(minutes=1)
    offline_time = offline_due + timedelta(days=3)
    engine = build_engine(settings.database_url)
    set_due(engine, (1,), offline_due)
    engine.dispose()
    restarted = create_app(
        settings,
        smtp_transport=transport,
        now_provider=lambda: offline_time,
    )
    with TestClient(restarted) as client:
        with Session(restarted.state.engine) as session:
            missed = session.exec(
                select(ScheduledCrawlEntry).where(
                    ScheduledCrawlEntry.site_id == 1,
                    ScheduledCrawlEntry.status == MISSED,
                )
            ).one()
        assert missed.missed_periods == 4
        before = hashlib.sha256(database_path.read_bytes()).hexdigest()
        assert client.get("/").status_code == 200
        assert client.get("/sites/1/schedule").status_code == 200
        after = hashlib.sha256(database_path.read_bytes()).hexdigest()
        assert after == before

    assert "/robots.txt" in requests
    assert "/site1" in requests and "/site2/bad" in requests
    assert get_schedule(restarted.state.engine, 1).next_run_at > offline_time
