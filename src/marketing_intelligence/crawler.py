"""Последовательное ядро ограниченного BFS-обхода одного origin."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .link_discovery import (
    MAX_DISCOVERED_LINKS,
    extract_internal_links,
    normalize_http_url,
)


USER_AGENT = (
    "MarketingIntelligenceBot/0.1 "
    "(+https://github.com/Jenechek/Marketing-Intelligence-Architect)"
)
REQUEST_TIMEOUT_SECONDS = 15.0
REQUEST_DELAY_SECONDS = 1.0
MAX_HTML_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_DEPTH = 3


class CrawlStatus(StrEnum):
    COMPLETED = "completed"
    ROBOTS_REDIRECT = "robots_redirect"
    ROBOTS_DEFERRED = "robots_deferred"
    ROBOTS_NETWORK_ERROR = "robots_network_error"
    ROBOTS_TIMEOUT = "robots_timeout"


class PageOutcome(StrEnum):
    HTML = "html"
    NON_HTML = "non_html"
    OVERSIZED = "oversized"
    PARSE_ERROR = "parse_error"
    FORBIDDEN = "forbidden"
    REDIRECT = "redirect"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class CrawlSettings:
    max_pages: int = DEFAULT_MAX_PAGES
    max_depth: int = DEFAULT_MAX_DEPTH
    delay: float = REQUEST_DELAY_SECONDS
    timeout: float = REQUEST_TIMEOUT_SECONDS
    user_agent: str = USER_AGENT

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_pages, bool)
            or not isinstance(self.max_pages, int)
            or self.max_pages < 1
        ):
            raise ValueError("max_pages должен быть положительным целым числом.")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or self.max_depth < 0
        ):
            raise ValueError("max_depth должен быть неотрицательным целым числом.")
        if (
            isinstance(self.delay, bool)
            or not isinstance(self.delay, (int, float))
            or not math.isfinite(self.delay)
            or self.delay < 0
        ):
            raise ValueError("delay должен быть конечным неотрицательным числом.")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("timeout должен быть конечным положительным числом.")
        if not self.user_agent.strip():
            raise ValueError("user_agent не должен быть пустым.")


@dataclass(frozen=True)
class CrawlPageResult:
    url: str
    depth: int
    outcome: PageOutcome
    message: str
    http_status: int | None = None
    discovered_links: tuple[str, ...] = ()
    links_limited: bool = False


@dataclass(frozen=True)
class CrawlCounters:
    processed: int = 0
    requested: int = 0
    successful: int = 0
    forbidden: int = 0
    errors: int = 0


@dataclass(frozen=True)
class CrawlResult:
    status: CrawlStatus
    message: str
    robots_status: int | None
    pages: tuple[CrawlPageResult, ...]
    counters: CrawlCounters
    limited: bool = False


ClientFactory = Callable[..., Any]
DelayFunction = Callable[[float], Awaitable[None]]
ProgressFunction = Callable[[CrawlCounters], Awaitable[None]]


class Crawler:
    """Обходить URL строго последовательно и не хранить результат вне памяти."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = httpx.AsyncClient,
        transport: httpx.AsyncBaseTransport | None = None,
        delay: DelayFunction = asyncio.sleep,
    ) -> None:
        self._client_factory = client_factory
        self._transport = transport
        self._delay = delay
        self._lock = asyncio.Lock()

    async def crawl(
        self,
        start_url: str,
        settings: CrawlSettings | None = None,
        *,
        progress: ProgressFunction | None = None,
    ) -> CrawlResult:
        normalized_start = normalize_http_url(start_url)
        if normalized_start is None:
            raise ValueError("Стартовый URL должен быть корректным HTTP(S)-адресом.")
        active_settings = settings or CrawlSettings()
        async with self._lock:
            return await self._crawl_once(normalized_start, active_settings, progress)

    async def _crawl_once(
        self,
        start_url: str,
        settings: CrawlSettings,
        progress: ProgressFunction | None,
    ) -> CrawlResult:
        robots_url = _robots_url(start_url)
        client_options: dict[str, Any] = {
            "headers": {"User-Agent": settings.user_agent},
            "timeout": settings.timeout,
            "follow_redirects": False,
        }
        if self._transport is not None:
            client_options["transport"] = self._transport

        async with self._client_factory(**client_options) as client:
            robots_result = await self._load_robots(
                client,
                robots_url,
                settings.user_agent,
            )
            if isinstance(robots_result, CrawlResult):
                return robots_result
            robots_status, robots_parser = robots_result

            queue: deque[tuple[str, int]] = deque([(start_url, 0)])
            seen = {start_url}
            pages: list[CrawlPageResult] = []
            requested = 0
            limited = False

            while queue:
                url, depth = queue.popleft()
                if robots_parser is not None and not robots_parser.can_fetch(
                    settings.user_agent,
                    url,
                ):
                    pages.append(
                        CrawlPageResult(
                            url=url,
                            depth=depth,
                            outcome=PageOutcome.FORBIDDEN,
                            message="URL запрещён правилами robots.txt и не запрашивался.",
                        )
                    )
                    if progress is not None:
                        await progress(_count_pages(pages, requested))
                    continue

                await self._delay(settings.delay)
                requested += 1
                page, traversal_links = await self._request_page(client, url, depth)
                pages.append(page)
                if progress is not None:
                    await progress(_count_pages(pages, requested))

                if not traversal_links:
                    continue
                if depth >= settings.max_depth:
                    if any(link not in seen for link in traversal_links):
                        limited = True
                    continue
                for link in traversal_links:
                    if link in seen:
                        continue
                    if len(seen) >= settings.max_pages:
                        limited = True
                        continue
                    seen.add(link)
                    queue.append((link, depth + 1))

            counters = _count_pages(pages, requested)
            return CrawlResult(
                status=CrawlStatus.COMPLETED,
                message="Ограниченный обход завершён.",
                robots_status=robots_status,
                pages=tuple(pages),
                counters=counters,
                limited=limited,
            )

    async def _load_robots(
        self,
        client: httpx.AsyncClient,
        robots_url: str,
        user_agent: str,
    ) -> tuple[int, RobotFileParser | None] | CrawlResult:
        try:
            response = await client.get(robots_url)
        except httpx.TimeoutException:
            return _empty_result(
                CrawlStatus.ROBOTS_TIMEOUT,
                "Сервер не ответил на запрос robots.txt вовремя. Весь обход отложен.",
            )
        except httpx.RequestError:
            return _empty_result(
                CrawlStatus.ROBOTS_NETWORK_ERROR,
                "Не удалось получить robots.txt. Весь обход отложен.",
            )

        status = response.status_code
        if 300 <= status < 400:
            return _empty_result(
                CrawlStatus.ROBOTS_REDIRECT,
                "robots.txt перенаправляет запрос. Весь обход отложен.",
                status,
            )
        if status == 404:
            return status, None
        if not 200 <= status < 300:
            return _empty_result(
                CrawlStatus.ROBOTS_DEFERRED,
                "robots.txt временно недоступен или вернул неподдерживаемый ответ. Весь обход отложен.",
                status,
            )

        try:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            parser.can_fetch(user_agent, robots_url)
        except Exception:
            return _empty_result(
                CrawlStatus.ROBOTS_DEFERRED,
                "Не удалось разобрать robots.txt. Весь обход отложен.",
                status,
            )
        return status, parser

    async def _request_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
    ) -> tuple[CrawlPageResult, tuple[str, ...]]:
        response: httpx.Response | None = None
        try:
            request = client.build_request("GET", url)
            response = await client.send(request, stream=True)
            status = response.status_code
            if 300 <= status < 400:
                return (
                    CrawlPageResult(
                        url,
                        depth,
                        PageOutcome.REDIRECT,
                        "Страница перенаправляет запрос. Переход не выполнялся.",
                        status,
                    ),
                    (),
                )
            if not 200 <= status < 300:
                return (
                    CrawlPageResult(
                        url,
                        depth,
                        PageOutcome.HTTP_ERROR,
                        "Страница вернула ответ, который нельзя считать успешным.",
                        status,
                    ),
                    (),
                )
            return await _read_html_response(response, url, depth)
        except httpx.TimeoutException:
            return (
                CrawlPageResult(
                    url,
                    depth,
                    PageOutcome.TIMEOUT,
                    "Сервер не ответил за отведённое время.",
                    response.status_code if response is not None else None,
                ),
                (),
            )
        except httpx.RequestError:
            return (
                CrawlPageResult(
                    url,
                    depth,
                    PageOutcome.NETWORK_ERROR,
                    "Не удалось получить страницу из-за сетевой ошибки.",
                    response.status_code if response is not None else None,
                ),
                (),
            )
        finally:
            if response is not None:
                await response.aclose()


async def _read_html_response(
    response: httpx.Response,
    url: str,
    depth: int,
) -> tuple[CrawlPageResult, tuple[str, ...]]:
    status = response.status_code
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return (
            CrawlPageResult(
                url,
                depth,
                PageOutcome.NON_HTML,
                "Страница не является HTML. Внутренние ссылки не извлекались.",
                status,
            ),
            (),
        )

    declared_length = response.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > MAX_HTML_BYTES:
                return _oversized_result(url, depth, status), ()
        except ValueError:
            pass

    content = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = MAX_HTML_BYTES - len(content)
        if len(chunk) > remaining:
            return _oversized_result(url, depth, status), ()
        content.extend(chunk)

    try:
        encoding = response.encoding or "utf-8"
        html = bytes(content).decode(encoding, errors="replace")
        traversal_links, _ = extract_internal_links(html, url, limit=None)
    except Exception:
        return (
            CrawlPageResult(
                url,
                depth,
                PageOutcome.PARSE_ERROR,
                "Не удалось разобрать HTML страницы. Внутренние ссылки не извлечены.",
                status,
            ),
            (),
        )

    displayed_links = traversal_links[:MAX_DISCOVERED_LINKS]
    links_limited = len(traversal_links) > MAX_DISCOVERED_LINKS
    if not traversal_links:
        message = "На HTML-странице внутренние ссылки не найдены."
    elif links_limited:
        message = "Показаны первые 200 уникальных внутренних ссылок. Список ограничен."
    else:
        message = f"Найдено внутренних ссылок: {len(traversal_links)}."
    return (
        CrawlPageResult(
            url,
            depth,
            PageOutcome.HTML,
            message,
            status,
            displayed_links,
            links_limited,
        ),
        traversal_links,
    )


def _oversized_result(url: str, depth: int, status: int) -> CrawlPageResult:
    return CrawlPageResult(
        url,
        depth,
        PageOutcome.OVERSIZED,
        "HTML-страница превышает 2 МиБ. Внутренние ссылки не извлекались.",
        status,
    )


def _count_pages(pages: list[CrawlPageResult], requested: int) -> CrawlCounters:
    successful = {PageOutcome.HTML, PageOutcome.NON_HTML}
    errors = {
        PageOutcome.OVERSIZED,
        PageOutcome.PARSE_ERROR,
        PageOutcome.REDIRECT,
        PageOutcome.HTTP_ERROR,
        PageOutcome.NETWORK_ERROR,
        PageOutcome.TIMEOUT,
    }
    return CrawlCounters(
        processed=len(pages),
        requested=requested,
        successful=sum(page.outcome in successful for page in pages),
        forbidden=sum(page.outcome is PageOutcome.FORBIDDEN for page in pages),
        errors=sum(page.outcome in errors for page in pages),
    )


def _empty_result(
    status: CrawlStatus,
    message: str,
    robots_status: int | None = None,
) -> CrawlResult:
    return CrawlResult(status, message, robots_status, (), CrawlCounters())


def _robots_url(start_url: str) -> str:
    parsed = urlsplit(start_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
