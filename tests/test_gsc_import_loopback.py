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
from marketing_intelligence.models import GSCImport, GSCPageMetric


def _token(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_actual_fastapi_loopback_crawl_and_two_gsc_imports(tmp_path: Path, monkeypatch):
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    class SiteHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
                return
            body = (
                b'<html><body><a href="/found">found</a></body></html>'
                if self.path == "/"
                else b"<html><body>found</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    site_server = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
    site_thread = threading.Thread(target=site_server.serve_forever, daemon=True)
    site_thread.start()
    root = f"http://127.0.0.1:{site_server.server_port}/"

    database = tmp_path / "data" / "loopback.db"
    settings = Settings(
        data_dir=database.parent,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database.as_posix()}",
    )
    app = create_app(
        settings, now_provider=lambda: datetime(2026, 2, 1, tzinfo=UTC)
    )
    app_port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=app_port, log_level="error")
    )
    app_thread = threading.Thread(target=server.run, daemon=True)
    app_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{app_port}",
            trust_env=False,
            follow_redirects=True,
        ) as client:
            created = client.post("/sites", data={"name": "Loopback", "url": root})
            assert created.status_code == 200

            crawl_screen = client.get("/sites/1/crawl")
            started = client.post(
                "/sites/1/crawl",
                data={
                    "action_token": _token(crawl_screen.text, "action_token"),
                    "max_pages": "2",
                    "max_depth": "1",
                    "delay": "0.5",
                    "timeout": "2",
                    "user_agent": "GSCImportLoopback/1.0",
                },
                follow_redirects=False,
            )
            assert started.status_code == 303
            for _ in range(300):
                crawl = client.get(started.headers["location"])
                if 'data-run-status="completed"' in crawl.text:
                    break
                time.sleep(0.02)
            assert 'data-run-status="completed"' in crawl.text

            first_csv = (
                "Page,Clicks,Impressions,Position,Extra\n"
                f'{root},2,10,1.2300,"<script>alert(1)</script>"\n'
                f"{root}found,1,5,2.5,x\n"
                f"{root}absent,0,0,,x\n"
            )
            import_screen = client.get("/sites/1/imports")
            preview = client.post(
                "/sites/1/imports/preview",
                data={
                    "action_token": _token(import_screen.text, "action_token"),
                    "period_start": "2026-01-01",
                    "period_end": "2026-01-31",
                    "report_confirmed": "yes",
                },
                files={"csv_file": ("Pages.csv", first_csv, "text/csv")},
            )
            assert preview.status_code == 200
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in preview.text
            first = client.post(
                "/sites/1/imports/confirm",
                data={
                    "preview_token": _token(preview.text, "preview_token"),
                    "action_token": _token(preview.text, "action_token"),
                    "page": "0", "clicks": "1", "impressions": "2", "position": "3",
                },
                follow_redirects=False,
            )
            assert first.status_code == 303

            second_csv = (
                "URL;Clicks;Impressions;Average position\n"
                f"{root};2;10;1.2300\n"
                f"{root}found;2;5;2.75\n"
                f"{root}new;1;4;3\n"
            )
            import_screen = client.get("/sites/1/imports")
            preview = client.post(
                "/sites/1/imports/preview",
                data={
                    "action_token": _token(import_screen.text, "action_token"),
                    "period_start": "2026-01-01",
                    "period_end": "2026-01-31",
                    "report_confirmed": "yes",
                },
                files={"csv_file": ("Pages.csv", second_csv, "text/csv")},
            )
            second = client.post(
                "/sites/1/imports/confirm",
                data={
                    "preview_token": _token(preview.text, "preview_token"),
                    "action_token": _token(preview.text, "action_token"),
                    "page": "0", "clicks": "1", "impressions": "2", "position": "3",
                },
                follow_redirects=False,
            )
            assert second.status_code == 303

            history = client.get("/sites/1/imports")
            metrics = client.get("/sites/1/gsc-pages")
            assert history.text.count("<td>Pages.csv</td>") == 2
            assert "добавлено 1, обновлено 1, без изменений 1" in history.text
            assert "Есть в последнем обходе" in metrics.text
            assert "Не найдена в последнем обходе" in metrics.text
            assert "20.00 %" in metrics.text
            assert "1.23" in metrics.text and "2.75" in metrics.text
            before = sha256(database.read_bytes()).hexdigest()
            assert client.get("/sites/1/imports").status_code == 200
            assert client.get("/sites/1/gsc-pages").status_code == 200
            assert sha256(database.read_bytes()).hexdigest() == before

        with Session(app.state.engine) as session:
            assert len(session.exec(select(GSCImport)).all()) == 2
            assert len(session.exec(select(GSCPageMetric)).all()) == 4
    finally:
        server.should_exit = True
        app_thread.join(timeout=10)
        site_server.shutdown()
        site_thread.join(timeout=5)
        site_server.server_close()
