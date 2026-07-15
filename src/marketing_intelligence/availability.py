"""Безопасная проверка robots.txt и одной стартовой страницы."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .link_discovery import extract_internal_links


USER_AGENT = (
    "MarketingIntelligenceBot/0.1 "
    "(+https://github.com/Jenechek/Marketing-Intelligence-Architect)"
)
REQUEST_TIMEOUT_SECONDS = 15.0
REQUEST_DELAY_SECONDS = 1.0
MAX_HTML_BYTES = 2 * 1024 * 1024


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    FORBIDDEN = "forbidden"
    DEFERRED = "deferred"
    REDIRECT = "redirect"
    NETWORK_ERROR = "network_error"


STATUS_TITLES = {
    AvailabilityStatus.AVAILABLE.value: "Доступно",
    AvailabilityStatus.FORBIDDEN.value: "Запрещено правилами robots.txt",
    AvailabilityStatus.DEFERRED.value: "Проверка отложена",
    AvailabilityStatus.REDIRECT.value: "Перенаправление",
    AvailabilityStatus.NETWORK_ERROR.value: "Сетевая ошибка",
    "running": "Проверка не завершена",
}


def status_title(status: str) -> str:
    """Вернуть понятное название сохранённого статуса."""

    return STATUS_TITLES.get(status, "Неизвестный результат")


@dataclass(frozen=True)
class AvailabilityResult:
    status: AvailabilityStatus
    title: str
    message: str
    robots_status: int | None = None
    page_status: int | None = None
    discovered_links: tuple[str, ...] = ()
    links_limited: bool = False
    discovery_message: str | None = None


ClientFactory = Callable[..., Any]
DelayFunction = Callable[[float], Awaitable[None]]


class AvailabilityChecker:
    """Выполнить не более одной проверки одновременно в процессе."""

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

    async def check(self, start_url: str) -> AvailabilityResult:
        async with self._lock:
            return await self._check_once(start_url)

    async def _check_once(self, start_url: str) -> AvailabilityResult:
        robots_url = _robots_url(start_url)
        robots_status: int | None = None
        client_options: dict[str, Any] = {
            "headers": {"User-Agent": USER_AGENT},
            "timeout": REQUEST_TIMEOUT_SECONDS,
            "follow_redirects": False,
        }
        if self._transport is not None:
            client_options["transport"] = self._transport

        try:
            async with self._client_factory(**client_options) as client:
                robots_result = await self._check_robots(client, robots_url, start_url)
                if isinstance(robots_result, AvailabilityResult):
                    return robots_result

                robots_status = robots_result
                await self._delay(REQUEST_DELAY_SECONDS)
                page_result = await self._check_start_page(client, start_url)
                return replace(page_result, robots_status=robots_result)
        except httpx.TimeoutException:
            return replace(
                _network_error(
                    "Сервер не ответил за 15 секунд. Проверку можно повторить позже."
                ),
                robots_status=robots_status,
            )
        except httpx.RequestError:
            return replace(
                _network_error(
                    "Не удалось подключиться к сайту. Проверку можно повторить позже."
                ),
                robots_status=robots_status,
            )

    async def _check_robots(
        self,
        client: httpx.AsyncClient,
        robots_url: str,
        start_url: str,
    ) -> AvailabilityResult | int:
        response = await client.get(robots_url)
        status = response.status_code

        if 300 <= status < 400:
            return AvailabilityResult(
                AvailabilityStatus.REDIRECT,
                "Перенаправление",
                "robots.txt перенаправляет запрос. Стартовая страница не запрашивалась.",
                robots_status=status,
            )
        if status == 404:
            return status
        if 200 <= status < 300:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            if parser.can_fetch(USER_AGENT, start_url):
                return status
            return AvailabilityResult(
                AvailabilityStatus.FORBIDDEN,
                "Запрещено правилами robots.txt",
                "Правила сайта запрещают запрос стартовой страницы для этого робота.",
                robots_status=status,
            )
        return AvailabilityResult(
            AvailabilityStatus.DEFERRED,
            "Проверка отложена",
            "robots.txt временно недоступен или вернул неподдерживаемый ответ. Стартовая страница не запрашивалась.",
            robots_status=status,
        )

    async def _check_start_page(
        self,
        client: httpx.AsyncClient,
        start_url: str,
    ) -> AvailabilityResult:
        request = client.build_request("GET", start_url)
        response = await client.send(request, stream=True)
        try:
            status = response.status_code
            if 300 <= status < 400:
                return AvailabilityResult(
                    AvailabilityStatus.REDIRECT,
                    "Перенаправление",
                    "Стартовая страница перенаправляет запрос. Переход не выполнялся.",
                    page_status=status,
                )
            if 200 <= status < 300:
                discovery = await _discover_from_response(response, start_url)
                return AvailabilityResult(
                    AvailabilityStatus.AVAILABLE,
                    "Доступно",
                    "robots.txt разрешает запрос, а стартовая страница ответила без перенаправления.",
                    page_status=status,
                    **discovery,
                )
            return AvailabilityResult(
                AvailabilityStatus.DEFERRED,
                "Проверка отложена",
                "Стартовая страница вернула ответ, который нельзя считать подтверждением доступности.",
                page_status=status,
            )
        finally:
            await response.aclose()


def _robots_url(start_url: str) -> str:
    parsed = urlsplit(start_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))


async def _discover_from_response(
    response: httpx.Response,
    start_url: str,
) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return {
            "discovery_message": "Стартовая страница не является HTML. Внутренние ссылки не извлекались."
        }

    declared_length = response.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > MAX_HTML_BYTES:
                return {
                    "discovery_message": "HTML-страница превышает 2 МиБ. Внутренние ссылки не извлекались."
                }
        except ValueError:
            pass

    content = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = MAX_HTML_BYTES - len(content)
        if len(chunk) > remaining:
            return {
                "discovery_message": "HTML-страница превышает 2 МиБ. Внутренние ссылки не извлекались."
            }
        content.extend(chunk)

    try:
        encoding = response.encoding or "utf-8"
        html = bytes(content).decode(encoding, errors="replace")
        links, limited = extract_internal_links(html, start_url)
    except Exception:
        return {
            "discovery_message": "Не удалось разобрать HTML стартовой страницы. Внутренние ссылки не извлечены."
        }

    if not links:
        message = "На стартовой HTML-странице внутренние ссылки не найдены."
    elif limited:
        message = "Показаны первые 200 уникальных внутренних ссылок. Список ограничен."
    else:
        message = f"Найдено внутренних ссылок: {len(links)}."
    return {
        "discovered_links": links,
        "links_limited": limited,
        "discovery_message": message,
    }


def _network_error(message: str) -> AvailabilityResult:
    return AvailabilityResult(
        AvailabilityStatus.NETWORK_ERROR,
        "Сетевая ошибка",
        message,
    )
