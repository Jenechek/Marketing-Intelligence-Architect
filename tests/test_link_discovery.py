from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

import marketing_intelligence.availability as availability_module
from marketing_intelligence.availability import (
    MAX_HTML_BYTES,
    extract_internal_links,
)
from test_availability import add_site, build_test_app, run_check


def test_filters_and_normalizes_internal_links_in_document_order() -> None:
    html = """
    <a href="/catalog/../products/">relative</a>
    <a href="HTTPS://EXAMPLE.COM:443/about#team">normalized</a>
    <a href="https://example.com:444/other">other port</a>
    <a href="http://example.com/insecure">other scheme</a>
    <a href="https://outside.example/page">external</a>
    <a href="https://user:secret@example.com/private">credentials</a>
    <a href="mailto:test@example.com">mail</a>
    <a href="javascript:void(0)">script</a>
    <a href="tel:+70000000000">phone</a>
    <a href="#section">start fragment</a>
    <a href="">empty</a>
    <link href="/not-an-anchor">
    """

    links, limited = extract_internal_links(
        html,
        "https://EXAMPLE.com:443/start#old",
    )

    assert links == (
        "https://example.com/products/",
        "https://example.com/about",
    )
    assert limited is False


def test_removes_exact_duplicates_after_normalization_and_keeps_query() -> None:
    html = """
    <a href="/one">one</a>
    <a href="https://EXAMPLE.com:443/one#top">duplicate</a>
    <a href="/one?view=full">query</a>
    <a href="./two/../one">duplicate relative</a>
    """

    links, limited = extract_internal_links(html, "https://example.com/start")

    assert links == (
        "https://example.com/one",
        "https://example.com/one?view=full",
    )
    assert limited is False


def test_link_limit_reports_that_more_unique_links_exist() -> None:
    html = "".join(f'<a href="/page-{index}">{index}</a>' for index in range(202))

    links, limited = extract_internal_links(html, "https://example.com/", limit=200)

    assert len(links) == 200
    assert links[0] == "https://example.com/page-0"
    assert links[-1] == "https://example.com/page-199"
    assert limited is True


def test_successful_html_is_read_and_links_are_shown_without_requesting_them(
    tmp_path: Path,
) -> None:
    html = b'<html><a href="/one">One</a><a href="/two#part">Two</a></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=html)

    app, _, requests = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)
        history_response = client.get("/sites/1/check")

    assert response.status_code == 200
    assert "Найдено внутренних ссылок: 2" in response.text
    assert "https://example.com/one" in response.text
    assert "https://example.com/two" in response.text
    assert [request.url.path for request in requests] == ["/robots.txt", "/start"]
    assert "https://example.com/one" not in history_response.text
    assert "https://example.com/two" not in history_response.text


def test_non_html_response_shows_message_without_reading_body(tmp_path: Path) -> None:
    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Тело не-HTML не должно читаться")
            yield b""

        async def aclose(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            stream=FailingStream(),
        )

    app, _, _ = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "не является HTML" in response.text


def test_html_over_two_mib_shows_message_and_no_links(tmp_path: Path) -> None:
    oversized = b"x" * MAX_HTML_BYTES + b'<a href="/hidden">hidden</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=oversized,
        )

    app, _, _ = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "превышает 2 МиБ" in response.text
    assert "https://example.com/hidden" not in response.text


def test_html_parse_failure_shows_controlled_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b'<a href="/one">one</a>',
        )

    def fail_to_parse(self, data: str) -> None:
        raise ValueError("controlled parser failure")

    monkeypatch.setattr(availability_module._HrefParser, "feed", fail_to_parse)
    app, _, _ = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert response.status_code == 200
    assert "Не удалось разобрать HTML" in response.text


def test_robots_forbid_prevents_html_and_link_requests(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /start")
        raise AssertionError("Стартовая страница и ссылки не должны запрашиваться")

    app, _, requests = build_test_app(tmp_path, handler)
    with TestClient(app) as client:
        add_site(client)
        response = run_check(client)

    assert "Запрещено правилами robots.txt" in response.text
    assert [request.url.path for request in requests] == ["/robots.txt"]
