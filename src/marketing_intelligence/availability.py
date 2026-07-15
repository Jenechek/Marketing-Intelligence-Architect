"""Адаптер существующей проверки доступности к ядру обхода."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum

import httpx

from .crawler import (
    MAX_HTML_BYTES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    ClientFactory,
    CrawlResult,
    CrawlSettings,
    Crawler,
    CrawlStatus,
    DelayFunction,
    PageOutcome,
)


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


class AvailabilityChecker:
    """Сохранить поведение проверки одной страницы через общее ядро."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = httpx.AsyncClient,
        transport: httpx.AsyncBaseTransport | None = None,
        delay: DelayFunction = asyncio.sleep,
    ) -> None:
        self._crawler = Crawler(
            client_factory=client_factory,
            transport=transport,
            delay=delay,
        )

    async def check(self, start_url: str) -> AvailabilityResult:
        crawl_result = await self._crawler.crawl(
            start_url,
            CrawlSettings(
                max_pages=1,
                max_depth=0,
                delay=REQUEST_DELAY_SECONDS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                user_agent=USER_AGENT,
            ),
        )
        return _to_availability_result(crawl_result)


def _to_availability_result(crawl: CrawlResult) -> AvailabilityResult:
    if crawl.status is CrawlStatus.ROBOTS_REDIRECT:
        return AvailabilityResult(
            AvailabilityStatus.REDIRECT,
            "Перенаправление",
            "robots.txt перенаправляет запрос. Стартовая страница не запрашивалась.",
            robots_status=crawl.robots_status,
        )
    if crawl.status is CrawlStatus.ROBOTS_DEFERRED:
        return AvailabilityResult(
            AvailabilityStatus.DEFERRED,
            "Проверка отложена",
            "robots.txt временно недоступен или вернул неподдерживаемый ответ. Стартовая страница не запрашивалась.",
            robots_status=crawl.robots_status,
        )
    if crawl.status is CrawlStatus.ROBOTS_TIMEOUT:
        return _network_error(
            "Сервер не ответил за 15 секунд. Проверку можно повторить позже.",
            crawl.robots_status,
        )
    if crawl.status is CrawlStatus.ROBOTS_NETWORK_ERROR:
        return _network_error(
            "Не удалось подключиться к сайту. Проверку можно повторить позже.",
            crawl.robots_status,
        )

    page = crawl.pages[0]
    if page.outcome is PageOutcome.FORBIDDEN:
        return AvailabilityResult(
            AvailabilityStatus.FORBIDDEN,
            "Запрещено правилами robots.txt",
            "Правила сайта запрещают запрос стартовой страницы для этого робота.",
            robots_status=crawl.robots_status,
        )
    if page.outcome is PageOutcome.REDIRECT:
        return AvailabilityResult(
            AvailabilityStatus.REDIRECT,
            "Перенаправление",
            "Стартовая страница перенаправляет запрос. Переход не выполнялся.",
            robots_status=crawl.robots_status,
            page_status=page.http_status,
        )
    if page.outcome is PageOutcome.HTTP_ERROR:
        return AvailabilityResult(
            AvailabilityStatus.DEFERRED,
            "Проверка отложена",
            "Стартовая страница вернула ответ, который нельзя считать подтверждением доступности.",
            robots_status=crawl.robots_status,
            page_status=page.http_status,
        )
    if page.outcome is PageOutcome.TIMEOUT:
        return _network_error(
            "Сервер не ответил за 15 секунд. Проверку можно повторить позже.",
            crawl.robots_status,
        )
    if page.outcome is PageOutcome.NETWORK_ERROR:
        return _network_error(
            "Не удалось подключиться к сайту. Проверку можно повторить позже.",
            crawl.robots_status,
        )

    return AvailabilityResult(
        AvailabilityStatus.AVAILABLE,
        "Доступно",
        "robots.txt разрешает запрос, а стартовая страница ответила без перенаправления.",
        robots_status=crawl.robots_status,
        page_status=page.http_status,
        discovered_links=page.discovered_links,
        links_limited=page.links_limited,
        discovery_message=page.message,
    )


def _network_error(
    message: str,
    robots_status: int | None,
) -> AvailabilityResult:
    return AvailabilityResult(
        AvailabilityStatus.NETWORK_ERROR,
        "Сетевая ошибка",
        message,
        robots_status=robots_status,
    )
