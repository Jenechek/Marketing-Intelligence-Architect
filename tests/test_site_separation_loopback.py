from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import socket
import threading
import time

import httpx
from sqlmodel import Session, select
import uvicorn

from marketing_intelligence.config import Settings
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    CrawlRun,
    GSCImport,
    GSCPageMetric,
    Site,
    SiteSchedule,
    SnapshotChangeEvent,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _token(html: str, name: str = "action_token") -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _transfer_form(html: str) -> dict[str, str]:
    result = {"action_token": _token(html)}
    for name in ("source_type", "target_type"):
        match = re.search(rf'name="{name}" value="([^"]+)"', html)
        assert match is not None
        result[name] = match.group(1)
    return result


def _start_app(app):
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    return server, thread, port


def _completed_crawl(client: httpx.Client, site_id: int) -> None:
    screen = client.get(f"/sites/{site_id}/crawl")
    response = client.post(
        f"/sites/{site_id}/crawl",
        data={
            "action_token": _token(screen.text),
            "max_pages": "1",
            "max_depth": "0",
            "delay": "0.5",
            "timeout": "3",
            "user_agent": "Task0036Loopback/1.0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    for _ in range(400):
        progress = client.get(response.headers["location"])
        if 'data-run-status="completed"' in progress.text:
            return
        time.sleep(0.02)
    raise AssertionError("Loopback-обход не завершился вовремя.")


def test_actual_loopback_separates_sites_gsc_histories_transfers_and_restart(
    tmp_path: Path,
    monkeypatch,
):
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    version = {"value": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
                return
            title = "Первая версия" if version["value"] == 1 else "Вторая <версия>"
            body = f"<html><head><title>{title}</title></head><body>текст</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    root = f"http://127.0.0.1:{target.server_port}/"
    database = tmp_path / "data" / "loopback.db"
    settings = Settings(
        data_dir=database.parent,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database.as_posix()}",
    )
    app = create_app(settings, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    server, thread, port = _start_app(app)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            follow_redirects=True,
        ) as client:
            client.post(
                "/competitors",
                data={"name": "<script>Конкурент</script>", "url": root},
            )
            client.post(
                "/own-sites",
                data={"name": "Свой без GSC", "url": root},
            )
            client.post(
                "/own-sites",
                data={"name": "Свой с GSC", "url": root},
            )
            competitors = client.get("/competitors")
            own_sites = client.get("/own-sites")
            assert "&lt;script&gt;Конкурент&lt;/script&gt;" in competitors.text
            assert "Свой без GSC" not in competitors.text
            assert "Свой без GSC" in own_sites.text
            assert client.get("/own-sites/1/imports").status_code == 404

            with Session(app.state.engine) as session:
                session.add(
                    SiteSchedule(
                        site_id=1,
                        enabled=False,
                        frequency="weekly",
                        local_weekday=0,
                        local_time="09:00",
                        max_pages=1,
                        max_depth=0,
                        delay=0.5,
                        timeout=3,
                        user_agent="Task0036Loopback/1.0",
                    )
                )
                session.commit()

            _completed_crawl(client, 1)
            _completed_crawl(client, 2)
            version["value"] = 2
            _completed_crawl(client, 1)
            _completed_crawl(client, 2)

            import_screen = client.get("/own-sites/3/imports")
            preview = client.post(
                "/own-sites/3/imports/preview",
                data={
                    "action_token": _token(import_screen.text),
                    "period_start": "2026-01-01",
                    "period_end": "2026-01-31",
                    "report_confirmed": "yes",
                },
                files={
                    "csv_file": (
                        "Pages.csv",
                        f"Page,Clicks,Impressions\n{root}page,2,10\n".encode(),
                        "text/csv",
                    )
                },
            )
            confirmed = client.post(
                "/own-sites/3/imports/confirm",
                data={
                    "preview_token": _token(preview.text, "preview_token"),
                    "action_token": _token(preview.text),
                    "page": "0",
                    "clicks": "1",
                    "impressions": "2",
                    "position": "",
                },
            )
            assert confirmed.status_code == 200
            assert "Pages.csv" in client.get("/own-sites/3/imports").text
            assert "20.00 %" in client.get("/own-sites/3/gsc-pages").text

            before_competitor_history = client.get("/competitors/changes")
            before_owned_history = client.get("/own-sites/changes")
            assert "&lt;script&gt;Конкурент&lt;/script&gt;" in before_competitor_history.text
            assert "Свой без GSC" not in before_competitor_history.text
            assert "Свой без GSC" in before_owned_history.text

            move_one = client.get("/sites/1/transfer")
            moved_one = client.post(
                "/sites/1/transfer",
                data=_transfer_form(move_one.text),
                follow_redirects=False,
            )
            move_two = client.get("/sites/2/transfer")
            moved_two = client.post(
                "/sites/2/transfer",
                data=_transfer_form(move_two.text),
                follow_redirects=False,
            )
            blocked_screen = client.get("/sites/3/transfer")
            blocked = client.post(
                "/sites/3/transfer",
                data=_transfer_form(blocked_screen.text),
                follow_redirects=False,
            )
            assert moved_one.headers["location"] == "/own-sites?transferred=1"
            assert moved_two.headers["location"] == "/competitors?transferred=1"
            assert blocked.status_code == 409

            after_competitor_history = client.get("/competitors/changes?site_id=2")
            after_owned_history = client.get("/own-sites/changes?site_id=1")
            competitor_export = client.get("/competitors/changes/export.json").json()
            owned_export = client.get("/own-sites/changes/export.json").json()
            assert "Свой без GSC" in after_competitor_history.text
            assert "&lt;script&gt;Конкурент&lt;/script&gt;" in after_owned_history.text
            assert {event["site_id"] for event in competitor_export["events"]} == {2}
            assert {event["site_id"] for event in owned_export["events"]} == {1}

            with Session(app.state.engine) as session:
                assert [(site.id, site.site_type) for site in session.exec(select(Site).order_by(Site.id))] == [
                    (1, "owned"), (2, "competitor"), (3, "owned")
                ]
                assert len(session.exec(select(CrawlRun).where(CrawlRun.site_id == 1)).all()) == 2
                assert len(session.exec(select(CrawlRun).where(CrawlRun.site_id == 2)).all()) == 2
                assert session.exec(select(SiteSchedule).where(SiteSchedule.site_id == 1)).one()
                assert session.exec(select(SnapshotChangeEvent)).all()
                assert session.exec(select(GSCImport).where(GSCImport.site_id == 3)).one()
                assert session.exec(select(GSCPageMetric).where(GSCPageMetric.site_id == 3)).one()

            before_gets = sha256(database.read_bytes()).hexdigest()
            for path in (
                "/competitors", "/own-sites", "/competitors/changes",
                "/own-sites/changes", "/own-sites/3/imports", "/own-sites/3/gsc-pages",
            ):
                assert client.get(path).status_code == 200
            assert sha256(database.read_bytes()).hexdigest() == before_gets
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=5)

    restarted = create_app(
        settings,
        now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )
    restarted_server, restarted_thread, restarted_port = _start_app(restarted)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{restarted_port}",
            trust_env=False,
        ) as client:
            assert "Свой без GSC" in client.get("/competitors").text
            assert "&lt;script&gt;Конкурент&lt;/script&gt;" in client.get("/own-sites").text
            assert "Pages.csv" in client.get("/own-sites/3/imports").text
    finally:
        restarted_server.should_exit = True
        restarted_thread.join(timeout=10)
