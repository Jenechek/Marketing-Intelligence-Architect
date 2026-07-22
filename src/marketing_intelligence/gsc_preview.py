"""Ограниченное одноразовое состояние предпросмотра импорта в одном процессе."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
import secrets
from threading import Lock
from typing import Callable

from .gsc_csv import ParsedCSV


PREVIEW_TTL = timedelta(minutes=30)
MAX_PREVIEWS = 20


@dataclass(frozen=True)
class ImportPreview:
    token: str
    site_id: int
    period_start: date
    period_end: date
    parsed: ParsedCSV
    created_at: datetime
    in_use: bool = False


class PreviewStore:
    """Потокобезопасное хранилище с TTL и одноразовым захватом токена."""

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
        max_previews: int = MAX_PREVIEWS,
    ) -> None:
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._max_previews = max_previews
        self._entries: dict[str, ImportPreview] = {}
        self._lock = Lock()

    def add(self, site_id: int, period_start: date, period_end: date, parsed: ParsedCSV) -> ImportPreview:
        with self._lock:
            now = self._aware_now()
            self._purge(now)
            while len(self._entries) >= self._max_previews:
                oldest = min(self._entries.values(), key=lambda item: item.created_at)
                del self._entries[oldest.token]
            preview = ImportPreview(
                token=secrets.token_urlsafe(32),
                site_id=site_id,
                period_start=period_start,
                period_end=period_end,
                parsed=parsed,
                created_at=now,
            )
            self._entries[preview.token] = preview
            return preview

    def get(self, token: str, site_id: int) -> ImportPreview | None:
        with self._lock:
            self._purge(self._aware_now())
            preview = self._entries.get(token)
            if preview is None or preview.site_id != site_id or preview.in_use:
                return None
            return preview

    def acquire(self, token: str, site_id: int) -> ImportPreview | None:
        with self._lock:
            self._purge(self._aware_now())
            preview = self._entries.get(token)
            if preview is None or preview.site_id != site_id or preview.in_use:
                return None
            acquired = replace(preview, in_use=True)
            self._entries[token] = acquired
            return acquired

    def release(self, token: str) -> None:
        with self._lock:
            preview = self._entries.get(token)
            if preview is not None:
                self._entries[token] = replace(preview, in_use=False)

    def consume(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def _purge(self, now: datetime) -> None:
        expired = [
            token
            for token, preview in self._entries.items()
            if now - preview.created_at >= PREVIEW_TTL
        ]
        for token in expired:
            del self._entries[token]

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)
