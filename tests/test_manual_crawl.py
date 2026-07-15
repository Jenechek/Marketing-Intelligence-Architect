import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from marketing_intelligence.config import Settings
from marketing_intelligence.crawl_history import start_crawl_run
from marketing_intelligence.crawler import (
    CrawlCounters,
    CrawlPageResult,
    CrawlResult,
    CrawlSettings,
    CrawlStatus,
    PageOutcome,
)
from marketing_intelligence.main import create_app
from marketing_intelligence.models import CrawlPageRecord, CrawlRun, Site


def build_app(tmp_path: Path, crawler=None):
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
    )
    return create_app(settings, crawler=crawler)


def add_site(client: TestClient, url: str = "https://example.com/") -> None:
    response = client.post(
        "/sites",
        data={"name": "Тестовый сайт", "url": url},
        follow_redirects=False,
    )
    assert response.status_code == 303


def crawl_token(client: TestClient, site_id: int = 1) -> str:
    response = client.get(f"/sites/{site_id}/crawl")
    assert response.status_code == 200
    assert response.text.count('class="primary-action"') == 1
    assert "Страниц не более</dt><dd>200" in response.text
    assert "Глубина</dt><dd>3" in response.text
    assert "Задержка</dt><dd>1 с" in response.text
    assert "Ожидание ответа</dt><dd>15 с" in response.text
    assert "Одновременных запросов</dt><dd>1" in response.text
    match = re.search(r'name="action_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def delete_token(client: TestClient, site_id: int) -> str:
    response = client.get(f"/sites/{site_id}/delete")
    assert response.status_code == 200
    match = re.search(r'name="confirmation_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def saved_runs(app) -> list[CrawlRun]:
    with Session(app.state.engine) as session:
        return list(session.exec(select(CrawlRun).order_by(CrawlRun.id)).all())


class BlockingCrawler:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def crawl(self, start_url, settings, *, progress=None):
        self.calls += 1
        self.started.set()
        if progress is not None:
            await progress(CrawlCounters(processed=1, requested=1, successful=1))
        await asyncio.to_thread(self.release.wait, 5)
        page = CrawlPageResult(
            start_url,
            0,
            PageOutcome.HTML,
            "Страница обработана.",
            200,
        )
        return CrawlResult(
            CrawlStatus.COMPLETED,
            "Ограниченный обход завершён.",
            404,
            (page,),
            CrawlCounters(processed=1, requested=1, successful=1),
            False,
        )


def test_background_progress_completion_and_ui_availability(tmp_path: Path) -> None:
    crawler = BlockingCrawler()
    app = build_app(tmp_path, crawler)

    with TestClient(app) as client:
        add_site(client)
        token = crawl_token(client)
        response = client.post(
            "/sites/1/crawl",
            data={"action_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/crawl-runs/1"
        assert crawler.started.wait(2)

        home = client.get("/")
        progress = client.get("/crawl-runs/1")
        with Session(app.state.engine) as session:
            page_records_while_running = session.exec(select(CrawlPageRecord)).all()

        assert home.status_code == 200
        assert "Добавленные сайты" in home.text
        assert progress.status_code == 200
        assert '<meta http-equiv="refresh" content="1">' in progress.text
        assert "1</strong> / 200 страниц обработано" in progress.text
        assert "Запрошено</dt><dd>1" in progress.text
        assert page_records_while_running == []

        crawler.release.set()
        for _ in range(100):
            completed = client.get("/crawl-runs/1")
            if 'data-run-status="completed"' in completed.text:
                break
            time.sleep(0.01)

        assert 'data-run-status="completed"' in completed.text
        assert 'http-equiv="refresh"' not in completed.text
        assert "Ограничение обхода не достигнуто" in completed.text
        with Session(app.state.engine) as session:
            assert len(session.exec(select(CrawlPageRecord)).all()) == 1


def test_duplicate_start_redirects_to_active_run_without_new_network(tmp_path: Path) -> None:
    crawler = BlockingCrawler()
    app = build_app(tmp_path, crawler)

    with TestClient(app) as client:
        add_site(client)
        client.post(
            "/sites",
            data={"name": "Второй сайт", "url": "https://example.org/"},
        )
        token = crawl_token(client)
        second_site_token = crawl_token(client, 2)
        first = client.post(
            "/sites/2/crawl",
            data={"action_token": second_site_token},
            follow_redirects=False,
        )
        assert crawler.started.wait(2)
        second = client.post(
            "/sites/1/crawl", data={"action_token": token}, follow_redirects=False
        )
        message = client.get(second.headers["location"])
        crawler.release.set()

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == "/crawl-runs/1?duplicate=1"
    assert "Полный обход уже выполняется" in message.text
    assert crawler.calls == 1
    assert len(saved_runs(app)) == 1


def test_invalid_token_and_unknown_ids_have_no_side_effects(tmp_path: Path) -> None:
    crawler = BlockingCrawler()
    app = build_app(tmp_path, crawler)

    with TestClient(app) as client:
        add_site(client)
        check_screen = client.get("/sites/1/check")
        check_token_match = re.search(
            r'name="action_token" value="([^"]+)"', check_screen.text
        )
        assert check_token_match is not None
        forbidden = client.post(
            "/sites/1/crawl", data={"action_token": check_token_match.group(1)}
        )
        unknown_get = client.get("/sites/999/crawl")
        unknown_post = client.post("/sites/999/crawl", data={"action_token": "x"})
        unknown_run = client.get("/crawl-runs/999")

    assert forbidden.status_code == 403
    assert "Токен запуска недействителен" in forbidden.text
    assert unknown_get.status_code == 404
    assert unknown_post.status_code == 404
    assert unknown_run.status_code == 404
    assert crawler.calls == 0
    assert saved_runs(app) == []


def test_shutdown_cancels_tracked_task_and_next_startup_marks_interrupted(
    tmp_path: Path,
) -> None:
    class NeverFinishingCrawler:
        def __init__(self) -> None:
            self.started = threading.Event()

        async def crawl(self, start_url, settings, *, progress=None):
            self.started.set()
            await asyncio.Event().wait()

    crawler = NeverFinishingCrawler()
    app = build_app(tmp_path, crawler)
    with TestClient(app) as client:
        add_site(client)
        response = client.post(
            "/sites/1/crawl",
            data={"action_token": crawl_token(client)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert crawler.started.wait(2)

    restarted = build_app(tmp_path)
    with TestClient(restarted) as client:
        result = client.get("/crawl-runs/1")

    assert result.status_code == 200
    assert 'data-run-status="interrupted"' in result.text
    assert "Прерван" in result.text
    assert 'http-equiv="refresh"' not in result.text


def test_running_crawl_blocks_only_its_site_deletion_and_prevents_data_mixing(
    tmp_path: Path,
) -> None:
    crawler = BlockingCrawler()
    app = build_app(tmp_path, crawler)

    with TestClient(app) as client:
        add_site(client, "https://old.example/")
        client.post(
            "/sites",
            data={"name": "Удаляемый второй", "url": "https://second.example/"},
        )
        old_delete_token = delete_token(client, 1)
        second_delete_token = delete_token(client, 2)
        started = client.post(
            "/sites/1/crawl",
            data={"action_token": crawl_token(client, 1)},
            follow_redirects=False,
        )
        assert started.status_code == 303
        assert crawler.started.wait(2)

        blocked_get = client.get("/sites/1/delete")
        blocked_post = client.post(
            "/sites/1/delete",
            data={"confirmation_token": old_delete_token},
            follow_redirects=False,
        )
        second_delete = client.post(
            "/sites/2/delete",
            data={"confirmation_token": second_delete_token},
            follow_redirects=False,
        )
        client.post(
            "/sites",
            data={"name": "Новый сайт", "url": "https://new.example/"},
        )

        assert blocked_get.status_code == 200
        assert "Дождитесь завершения обхода" in blocked_get.text
        assert 'href="/crawl-runs/1"' in blocked_get.text
        assert 'method="post"' not in blocked_get.text
        assert 'name="confirmation_token"' not in blocked_get.text
        assert blocked_post.status_code == 409
        assert "Сайт «Тестовый сайт» не удалён" in blocked_post.text
        assert second_delete.status_code == 303

        with Session(app.state.engine) as session:
            sites_while_running = list(session.exec(select(Site).order_by(Site.id)))
            run_while_running = session.get(CrawlRun, 1)
        assert [(site.id, site.url) for site in sites_while_running] == [
            (1, "https://old.example/"),
            (2, "https://new.example/"),
        ]
        assert run_while_running is not None
        assert run_while_running.site_id == 1

        crawler.release.set()
        for _ in range(100):
            completed = client.get("/crawl-runs/1")
            if 'data-run-status="completed"' in completed.text:
                break
            time.sleep(0.01)

        with Session(app.state.engine) as session:
            records = list(session.exec(select(CrawlPageRecord)).all())
        assert [record.url for record in records] == ["https://old.example/"]
        assert all("new.example" not in record.url for record in records)

        completed_delete_token = delete_token(client, 1)
        completed_delete = client.post(
            "/sites/1/delete",
            data={"confirmation_token": completed_delete_token},
            follow_redirects=False,
        )
        remaining = client.get("/")

    assert completed_delete.status_code == 303
    assert "https://old.example/" not in remaining.text
    assert "https://new.example/" in remaining.text


def test_delete_guard_uses_persisted_running_status_without_background_task(
    tmp_path: Path,
) -> None:
    app = build_app(tmp_path)

    with TestClient(app) as client:
        add_site(client)
        token = delete_token(client, 1)
        run = start_crawl_run(app.state.engine, 1, CrawlSettings(delay=0))
        get_response = client.get("/sites/1/delete")
        post_response = client.post(
            "/sites/1/delete",
            data={"confirmation_token": token},
        )

    assert run.id == 1
    assert get_response.status_code == 200
    assert 'href="/crawl-runs/1"' in get_response.text
    assert post_response.status_code == 409
    with Session(app.state.engine) as session:
        assert session.get(Site, 1) is not None
        assert session.get(CrawlRun, 1) is not None


def test_real_loopback_crawl_finishes_through_server_ui(tmp_path: Path) -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
                return
            body = b"<html><body>ok</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app = build_app(tmp_path)
    try:
        with TestClient(app) as client:
            add_site(client, f"http://127.0.0.1:{server.server_port}/")
            response = client.post(
                "/sites/1/crawl",
                data={"action_token": crawl_token(client)},
                follow_redirects=False,
            )
            assert response.status_code == 303
            for _ in range(300):
                result = client.get(response.headers["location"])
                if 'data-run-status="completed"' in result.text:
                    break
                time.sleep(0.01)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert 'data-run-status="completed"' in result.text
    assert "1</strong> / 200 страниц обработано" in result.text
    assert requests == ["/robots.txt", "/"]
