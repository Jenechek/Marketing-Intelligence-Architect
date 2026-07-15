import asyncio
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlmodel import Session, select

from marketing_intelligence.availability import (
    AvailabilityChecker,
    AvailabilityStatus,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from marketing_intelligence.config import Settings
from marketing_intelligence.main import create_app
from marketing_intelligence.models import AvailabilityCheck


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.was_read = False
        self.was_closed = False

    async def __aiter__(self):
        self.was_read = True
        yield self.content

    async def aclose(self) -> None:
        self.was_closed = True


def build_test_app(
    tmp_path: Path,
    handler,
    *,
    delays: list[float] | None = None,
) -> tuple[FastAPI, Path, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    async def record_delay(seconds: float) -> None:
        if delays is not None:
            delays.append(seconds)

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    data_dir = tmp_path / "data"
    database_path = data_dir / "test.db"
    settings = Settings(
        data_dir=data_dir,
        logs_dir=tmp_path / "logs",
        database_url=f"sqlite:///{database_path.as_posix()}",
    )
    checker = AvailabilityChecker(
        transport=httpx.MockTransport(recording_handler),
        delay=record_delay,
    )
    return create_app(settings, availability_checker=checker), database_path, requests


def add_site(client: TestClient, url: str = "https://example.com/start?source=test") -> None:
    response = client.post(
        "/sites",
        data={"name": "Проверяемый сайт", "url": url},
        follow_redirects=False,
    )
    assert response.status_code == 303


def get_action_token(client: TestClient, site_id: int = 1) -> str:
    response = client.get(f"/sites/{site_id}/check")
    assert response.status_code == 200
    match = re.search(r'name="action_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def run_check(client: TestClient, site_id: int = 1) -> httpx.Response:
    return client.post(
        f"/sites/{site_id}/check",
        data={"action_token": get_action_token(client, site_id)},
    )


def saved_checks(app: FastAPI) -> list[AvailabilityCheck]:
    with Session(app.state.engine) as session:
        statement = select(AvailabilityCheck).order_by(AvailabilityCheck.id)
        return list(session.exec(statement).all())


def test_check_screen_has_site_and_one_primary_action(tmp_path: Path) -> None:
    app, _, requests = build_test_app(
        tmp_path,
        lambda request: httpx.Response(500),
    )
    with TestClient(app) as client:
        add_site(client)
        list_response = client.get("/")
        response = client.get("/sites/1/check")
        checks = saved_checks(app)

    assert response.status_code == 200
    assert "Проверяемый сайт" in response.text
    assert "https://example.com/start?source=test" in response.text
    assert response.text.count('class="primary-action"') == 1
    assert 'method="post"' in response.text
    assert "Проверить доступность" in list_response.text
    assert requests == []
    assert checks == []


@pytest.mark.parametrize(
    ("robots_body", "expected_status", "page_requested"),
    [
        (b"User-agent: *\nAllow: /start", AvailabilityStatus.AVAILABLE, True),
        (b"User-agent: *\nDisallow: /start", AvailabilityStatus.FORBIDDEN, False),
    ],
)
def test_robots_allow_and_forbid(
    tmp_path: Path,
    robots_body: bytes,
    expected_status: AvailabilityStatus,
    page_requested: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=robots_body)
        return httpx.Response(204)

    app, _, requests = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)
        checks = saved_checks(app)

    assert response.status_code == 200
    assert f'class="notice {"success" if page_requested else "error"}' in response.text
    assert ("Доступно" if page_requested else "Запрещено правилами robots.txt") in response.text
    assert [request.url.path for request in requests].count("/start") == int(page_requested)
    assert checks[0].robots_status == 200
    assert checks[0].page_status == (204 if page_requested else None)


def test_missing_robots_allows_one_streamed_page_request(tmp_path: Path) -> None:
    page_stream = TrackingStream(b"body must not be read")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, stream=page_stream)

    delays: list[float] = []
    app, _, requests = build_test_app(tmp_path, handler, delays=delays)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "Доступно" in response.text
    assert [request.url.path for request in requests] == ["/robots.txt", "/start"]
    assert delays == [REQUEST_DELAY_SECONDS]
    assert page_stream.was_read is False
    assert page_stream.was_closed is True


@pytest.mark.parametrize("status_code", [401, 403, 500, 503, 418])
def test_unsupported_robots_response_defers_without_page_request(
    tmp_path: Path,
    status_code: int,
) -> None:
    app, _, requests = build_test_app(
        tmp_path,
        lambda request: httpx.Response(status_code),
    )
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "Проверка отложена" in response.text
    assert len(requests) == 1
    assert requests[0].url.path == "/robots.txt"


def test_robots_redirect_is_reported_without_following_or_page_request(tmp_path: Path) -> None:
    app, _, requests = build_test_app(
        tmp_path,
        lambda request: httpx.Response(302, headers={"Location": "/other"}),
    )
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "Перенаправление" in response.text
    assert len(requests) == 1
    assert requests[0].url.path == "/robots.txt"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_timeout_and_network_errors_are_controlled(
    tmp_path: Path,
    error_type: type[httpx.RequestError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("test failure", request=request)

    app, _, requests = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert response.status_code == 200
    assert "Сетевая ошибка" in response.text
    assert len(requests) == 1


def test_both_requests_use_approved_user_agent_timeout_and_no_redirects(tmp_path: Path) -> None:
    client_options: list[dict] = []

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            client_options.append(kwargs.copy())
            super().__init__(**kwargs)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404 if request.url.path == "/robots.txt" else 204)

    checker = AvailabilityChecker(
        client_factory=RecordingClient,
        transport=httpx.MockTransport(handler),
        delay=lambda seconds: asyncio.sleep(0),
    )
    result = asyncio.run(checker.check("https://example.com/start"))

    assert result.status is AvailabilityStatus.AVAILABLE
    assert len(requests) == 2
    assert all(request.headers["User-Agent"] == USER_AGENT for request in requests)
    assert all(request.extensions["timeout"]["read"] == REQUEST_TIMEOUT_SECONDS for request in requests)
    assert client_options[0]["follow_redirects"] is False
    assert client_options[0]["transport"] is not None


def test_start_page_redirect_is_not_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(301, headers={"Location": "/destination"})

    app, _, requests = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "Перенаправление" in response.text
    assert [request.url.path for request in requests] == ["/robots.txt", "/start"]


def test_invalid_missing_other_site_and_other_action_tokens_do_not_use_network(tmp_path: Path) -> None:
    app, _, requests = build_test_app(
        tmp_path,
        lambda request: httpx.Response(204),
    )
    with TestClient(app) as client:
        add_site(client)
        add_site(client, "https://example.org/")
        site_one_token = get_action_token(client, 1)
        delete_page = client.get("/sites/1/delete")
        delete_token = re.search(
            r'name="confirmation_token" value="([^"]+)"', delete_page.text
        ).group(1)

        responses = [
            client.post("/sites/1/check"),
            client.post("/sites/1/check", data={"action_token": "invalid"}),
            client.post("/sites/2/check", data={"action_token": site_one_token}),
            client.post("/sites/1/check", data={"action_token": delete_token}),
        ]
        checks = saved_checks(app)

    assert all(response.status_code == 403 for response in responses)
    assert all("Сетевые запросы не выполнялись" in response.text for response in responses)
    assert requests == []
    assert checks == []


def test_unknown_site_check_keeps_controlled_404_without_network(tmp_path: Path) -> None:
    app, _, requests = build_test_app(
        tmp_path,
        lambda request: httpx.Response(204),
    )
    with TestClient(app) as client:
        add_site(client)
        get_response = client.get("/sites/999/check")
        post_response = client.post("/sites/999/check")
        checks = saved_checks(app)
        saved_response = client.get("/")

    assert get_response.status_code == 404
    assert post_response.status_code == 404
    assert "Сайт не найден" in get_response.text
    assert requests == []
    assert checks == []
    assert "Проверяемый сайт" in saved_response.text


def test_successful_check_is_saved_with_times_status_message_and_http_codes(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path == "/robots.txt" else 204)

    app, _, _ = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)
        checks = saved_checks(app)

    assert response.status_code == 200
    assert len(checks) == 1
    assert checks[0].site_id == 1
    assert checks[0].started_at is not None
    assert checks[0].completed_at is not None
    assert checks[0].completed_at >= checks[0].started_at
    assert checks[0].status == AvailabilityStatus.AVAILABLE.value
    assert checks[0].message
    assert checks[0].robots_status == 404
    assert checks[0].page_status == 204
    assert "История проверок" in response.text
    assert "Ответ стартовой страницы: HTTP 204" in response.text


def test_network_error_is_saved_without_invented_http_codes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test failure", request=request)

    app, _, _ = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)
        checks = saved_checks(app)

    assert response.status_code == 200
    assert len(checks) == 1
    assert checks[0].status == AvailabilityStatus.NETWORK_ERROR.value
    assert "Не удалось подключиться" in checks[0].message
    assert checks[0].robots_status is None
    assert checks[0].page_status is None
    assert checks[0].completed_at is not None


def test_history_survives_restart_and_is_newest_first(tmp_path: Path) -> None:
    def first_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    app, _, _ = build_test_app(tmp_path, first_handler)
    with TestClient(app) as client:
        add_site(client)
        first_response = run_check(client)

    assert "Проверка отложена" in first_response.text

    def second_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200)

    restarted_app, _, _ = build_test_app(tmp_path, second_handler)
    with TestClient(restarted_app) as client:
        second_response = run_check(client)
        history_response = client.get("/sites/1/check")
        checks = saved_checks(restarted_app)

    assert second_response.status_code == 200
    assert history_response.status_code == 200
    assert len(checks) == 2
    assert history_response.text.count('class="history-item"') == 2
    assert history_response.text.index("Доступно") < history_response.text.index(
        "Проверка отложена"
    )


def test_checker_serializes_concurrent_checks() -> None:
    active = 0
    maximum_active = 0

    class ConcurrencyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(403)

    checker = AvailabilityChecker(transport=ConcurrencyTransport())

    async def run_both():
        return await asyncio.gather(
            checker.check("https://example.com/one"),
            checker.check("https://example.org/two"),
        )

    results = asyncio.run(run_both())

    assert [result.status for result in results] == [
        AvailabilityStatus.DEFERRED,
        AvailabilityStatus.DEFERRED,
    ]
    assert maximum_active == 1
