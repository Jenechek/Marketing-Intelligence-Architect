"""Async-диспетчер одной сохраняемой очереди полных обходов."""

import asyncio
from datetime import datetime, tzinfo
import logging

from sqlalchemy.engine import Engine

from .crawl_history import ActiveCrawlRunError, execute_crawl_run, start_crawl_run
from .crawler import Crawler
from .models import ScheduledCrawlEntry
from .scheduler import (
    COMPLETED,
    DEFERRED,
    FAILED,
    PARTIAL,
    claim_entry,
    complete_entry,
    entry_settings,
    get_entry,
    list_pending_notifications,
    next_pending_entry,
    release_entry,
    reserve_due_entries,
    set_notification_status,
)
from .sites import get_site
from .smtp_notifications import SMTPNotifier


NOTIFIABLE = {PARTIAL, DEFERRED, FAILED, "interrupted"}


class CrawlQueueDispatcher:
    """Последовательно выполняет очередь одного процесса FastAPI."""

    def __init__(
        self,
        engine: Engine,
        crawler: Crawler,
        notifier: SMTPNotifier,
        logger: logging.Logger,
        *,
        local_timezone: tzinfo | None = None,
    ) -> None:
        self.engine = engine
        self.crawler = crawler
        self.notifier = notifier
        self.logger = logger
        self.local_timezone = local_timezone
        self._task: asyncio.Task[None] | None = None
        self._wake_lock = asyncio.Lock()

    async def wake(self, *, now: datetime | None = None) -> None:
        """Зарезервировать срок и разбудить очередь, не храня график в APScheduler."""

        async with self._wake_lock:
            reserve_due_entries(
                self.engine,
                now=now,
                local_timezone=self.local_timezone,
            )
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(
                    self._drain(),
                    name="scheduled-crawl-queue",
                )

    async def wait_idle(self) -> None:
        task = self._task
        if task is not None:
            await task

    async def send_pending_notifications(self) -> None:
        """Доставить отдельные итоги, включая прерывания прошлого процесса."""

        for entry in list_pending_notifications(self.engine):
            if entry.id is None:
                continue
            site = get_site(self.engine, entry.site_id)
            if site is None:
                set_notification_status(self.engine, entry.id, "failed")
                continue
            await self._notify(entry.id, site)

    async def shutdown(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _drain(self) -> None:
        while True:
            entry = next_pending_entry(self.engine)
            if entry is None or entry.id is None:
                return
            if not claim_entry(self.engine, entry.id):
                continue
            site = get_site(self.engine, entry.site_id)
            if site is None:
                complete_entry(
                    self.engine,
                    entry.id,
                    status=FAILED,
                    message="Сайт для запуска не найден.",
                    notification_status="disabled",
                )
                continue
            settings = entry_settings(entry)
            try:
                run = start_crawl_run(self.engine, site.id, settings)
            except ActiveCrawlRunError:
                release_entry(self.engine, entry.id)
                return
            except Exception as error:
                complete_entry(
                    self.engine,
                    entry.id,
                    status=FAILED,
                    message=f"Запуск не создан: {error}",
                    notification_status="pending",
                )
                await self._notify(entry.id, site)
                continue
            if run.id is None:
                complete_entry(
                    self.engine,
                    entry.id,
                    status=FAILED,
                    message="Запуск обхода не получил идентификатор.",
                    notification_status="pending",
                )
                await self._notify(entry.id, site)
                continue
            from .scheduler import mark_entry_running

            mark_entry_running(self.engine, entry.id, run.id)
            try:
                completed = await execute_crawl_run(
                    self.engine,
                    run.id,
                    site.url,
                    crawler=self.crawler,
                    settings=settings,
                )
                status = completed.status
                message = completed.message
            except asyncio.CancelledError:
                raise
            except Exception as error:
                status = FAILED
                message = f"Обход завершился неожиданной ошибкой: {error}"
                self.logger.exception(
                    "Запланированный обход завершился ошибкой (entry_id=%s)",
                    entry.id,
                )
            notification = "pending" if status in NOTIFIABLE else "not_applicable"
            complete_entry(
                self.engine,
                entry.id,
                status=status,
                message=message,
                notification_status=notification,
            )
            if status in NOTIFIABLE:
                await self._notify(entry.id, site)

    async def _notify(self, entry_id: int, site) -> None:
        stored = get_entry(self.engine, entry_id)
        if stored is None:
            return
        notification_status = await self.notifier.send_entry(stored, site)
        set_notification_status(self.engine, entry_id, notification_status)
