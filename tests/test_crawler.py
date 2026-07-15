import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

from marketing_intelligence.crawler import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PAGES,
    MAX_HTML_BYTES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    CrawlSettings,
    Crawler,
    CrawlStatus,
    PageOutcome,
)


def run_crawl(handler, *, settings: CrawlSettings) -> tuple[object, list[str]]:
    requests: list[str] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return handler(request)

    crawler = Crawler(
        transport=httpx.MockTransport(recording_handler),
        delay=lambda seconds: asyncio.sleep(0),
    )
    result = asyncio.run(crawler.crawl("https://example.com/", settings))
    return result, requests


def html(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        content=body.encode(),
    )


def test_default_settings_match_approved_limits() -> None:
    settings = CrawlSettings()

    assert settings.max_pages == DEFAULT_MAX_PAGES == 200
    assert settings.max_depth == DEFAULT_MAX_DEPTH == 3
    assert settings.delay == REQUEST_DELAY_SECONDS == 1.0
    assert settings.timeout == REQUEST_TIMEOUT_SECONDS == 15.0
    assert settings.user_agent == USER_AGENT


def test_bfs_preserves_document_order() -> None:
    pages = {
        "/": '<a href="/a">a</a><a href="/b">b</a>',
        "/a": '<a href="/a-one">a1</a><a href="/a-two">a2</a>',
        "/b": '<a href="/b-one">b1</a>',
        "/a-one": "",
        "/a-two": "",
        "/b-one": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return html(pages[request.url.path])

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=20, max_depth=2, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/a", "/b", "/a-one", "/a-two", "/b-one"]
    assert [(page.url, page.depth) for page in result.pages] == [
        ("https://example.com/", 0),
        ("https://example.com/a", 1),
        ("https://example.com/b", 1),
        ("https://example.com/a-one", 2),
        ("https://example.com/a-two", 2),
        ("https://example.com/b-one", 2),
    ]


def test_max_pages_counts_forbidden_and_errors_and_max_depth_stops_deep_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /blocked")
        if request.url.path == "/":
            return html(
                '<a href="/blocked">blocked</a>'
                '<a href="/error">error</a>'
                '<a href="/extra">extra</a>'
            )
        if request.url.path == "/error":
            return httpx.Response(500)
        raise AssertionError(f"Лишний запрос: {request.url}")

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=3, max_depth=1, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/error"]
    assert [page.outcome for page in result.pages] == [
        PageOutcome.HTML,
        PageOutcome.FORBIDDEN,
        PageOutcome.HTTP_ERROR,
    ]
    assert result.counters.processed == 3
    assert result.counters.requested == 2
    assert result.limited is True


def test_deduplicates_normalized_urls_and_keeps_exact_same_origin() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return html(
                '<a href="/folder/../one#first">one</a>'
                '<a href="HTTPS://EXAMPLE.COM:443/one">duplicate</a>'
                '<a href="/one?view=full">query</a>'
                '<a href="https://example.com:444/wrong-port">port</a>'
                '<a href="http://example.com/wrong-scheme">scheme</a>'
                '<a href="https://other.example/out">external</a>'
                '<a href="https://user:secret@example.com/private">credentials</a>'
                '<a href="javascript:void(0)">js</a>'
            )
        return html("")

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=10, max_depth=1, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/one", "/one"]
    assert [page.url for page in result.pages] == [
        "https://example.com/",
        "https://example.com/one",
        "https://example.com/one?view=full",
    ]


def test_robots_forbidden_url_is_counted_but_never_requested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        if request.url.path == "/":
            return html('<a href="/private">private</a><a href="/public">public</a>')
        return html("")

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=10, max_depth=1, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/public"]
    assert result.pages[1].outcome is PageOutcome.FORBIDDEN
    assert result.counters.forbidden == 1


def test_redirect_external_and_too_deep_links_are_not_requested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return html(
                '<a href="/redirect">redirect</a>'
                '<a href="https://outside.example/page">external</a>'
                '<a href="/depth-one">depth</a>'
            )
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/target"})
        if request.url.path == "/depth-one":
            return html('<a href="/depth-two">too deep</a>')
        raise AssertionError(f"Лишний запрос: {request.url}")

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=10, max_depth=1, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/redirect", "/depth-one"]
    assert result.limited is True
    assert "https://example.com/target" not in [page.url for page in result.pages]
    assert "https://example.com/depth-two" not in [page.url for page in result.pages]


def test_one_page_network_error_does_not_stop_remaining_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return html('<a href="/broken">bad</a><a href="/good">good</a>')
        if request.url.path == "/broken":
            raise httpx.ConnectError("controlled", request=request)
        return html("")

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=10, max_depth=1, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/broken", "/good"]
    assert [page.outcome for page in result.pages] == [
        PageOutcome.HTML,
        PageOutcome.NETWORK_ERROR,
        PageOutcome.HTML,
    ]
    assert result.counters.errors == 1


def test_delay_precedes_every_sequential_page_request() -> None:
    events: list[str] = []
    active = 0
    maximum_active = 0

    class SequentialTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum_active
            events.append(f"request:{request.url.path}")
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if request.url.path == "/":
                return html('<a href="/a">a</a><a href="/b">b</a>')
            return html("")

    async def record_delay(seconds: float) -> None:
        events.append(f"delay:{seconds}")

    crawler = Crawler(transport=SequentialTransport(), delay=record_delay)
    asyncio.run(
        crawler.crawl(
            "https://example.com/",
            CrawlSettings(max_pages=3, max_depth=1, delay=0.25),
        )
    )

    assert events == [
        "request:/robots.txt",
        "delay:0.25",
        "request:/",
        "delay:0.25",
        "request:/a",
        "delay:0.25",
        "request:/b",
    ]
    assert maximum_active == 1


def test_streamed_two_mib_limit_records_error_and_continues() -> None:
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * MAX_HTML_BYTES
            yield b"x"

        async def aclose(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return html('<a href="/large">large</a><a href="/good">good</a>')
        if request.url.path == "/large":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                stream=OversizedStream(),
            )
        return html("")

    result, requests = run_crawl(
        handler,
        settings=CrawlSettings(max_pages=10, max_depth=1, delay=0),
    )

    assert requests == ["/robots.txt", "/", "/large", "/good"]
    assert result.pages[1].outcome is PageOutcome.OVERSIZED
    assert result.pages[2].outcome is PageOutcome.HTML


def test_real_local_server_confirms_exact_safe_request_list() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            responses = {
                "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /blocked"),
                "/": (
                    200,
                    "text/html",
                    '<a href="/allowed">ok</a>'
                    '<a href="/blocked">no</a>'
                    '<a href="https://outside.example/no">outside</a>'
                    '<a href="/redirect">redirect</a>'
                    '<a href="/level-one">level</a>',
                ),
                "/allowed": (200, "text/html", ""),
                "/redirect": (302, "text/plain", ""),
                "/level-one": (200, "text/html", '<a href="/too-deep">deep</a>'),
            }
            status, content_type, body = responses[self.path]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if self.path == "/redirect":
                self.send_header("Location", "/redirect-target")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        crawler = Crawler(delay=lambda seconds: asyncio.sleep(0))
        result = asyncio.run(
            crawler.crawl(
                f"http://127.0.0.1:{server.server_port}/",
                CrawlSettings(max_pages=20, max_depth=1, delay=0),
            )
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.status is CrawlStatus.COMPLETED
    assert requests == ["/robots.txt", "/", "/allowed", "/redirect", "/level-one"]
    assert "/blocked" not in requests
    assert "/redirect-target" not in requests
    assert "/too-deep" not in requests
