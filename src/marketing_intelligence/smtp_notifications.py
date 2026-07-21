"""Необязательные текстовые SMTP-уведомления без хранения секретов."""

import asyncio
from email.message import EmailMessage
import smtplib
import ssl
from typing import Protocol
import unicodedata

from .config import SMTPConfig
from .models import ScheduledCrawlEntry, Site
from .scheduler import status_title


class MailTransport(Protocol):
    def send(self, config: SMTPConfig, message: EmailMessage) -> None: ...


class SMTPTransport:
    """Синхронный стандартный транспорт, вызываемый вне async-потока."""

    def send(self, config: SMTPConfig, message: EmailMessage) -> None:
        if not config.enabled or config.host is None:
            raise ValueError("SMTP не настроен.")
        context = ssl.create_default_context()
        if config.security == "ssl":
            with smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=config.timeout,
                context=context,
            ) as client:
                self._authenticate_and_send(client, config, message)
            return
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as client:
            client.starttls(context=context)
            self._authenticate_and_send(client, config, message)

    @staticmethod
    def _authenticate_and_send(client, config: SMTPConfig, message: EmailMessage) -> None:
        if config.username is not None:
            assert config.password is not None
            client.login(config.username, config.password)
        client.send_message(message)


class SMTPNotifier:
    def __init__(
        self,
        config: SMTPConfig,
        transport: MailTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or SMTPTransport()

    async def send_entry(self, entry: ScheduledCrawlEntry, site: Site) -> str:
        """Отправить итог и вернуть отдельное состояние уведомления."""

        if not self.config.enabled:
            return "disabled"
        message = self._message(
            subject=(
                f"Marketing Intelligence: {status_title(entry.status)} — "
                f"{_safe_header_fragment(site.name)}"
            ),
            body=(
                f"Сайт: {site.name}\n"
                f"URL: {site.url}\n"
                f"Состояние: {status_title(entry.status)}\n"
                f"Сообщение: {entry.message}\n"
            ),
        )
        try:
            await asyncio.to_thread(self.transport.send, self.config, message)
        except Exception:
            return "failed"
        return "sent"

    async def send_test(self) -> bool:
        if not self.config.enabled:
            return False
        message = self._message(
            subject="Marketing Intelligence: тестовое письмо",
            body="SMTP-уведомления Marketing Intelligence настроены корректно.\n",
        )
        try:
            await asyncio.to_thread(self.transport.send, self.config, message)
        except Exception:
            return False
        return True

    def _message(self, *, subject: str, body: str) -> EmailMessage:
        assert self.config.from_address is not None
        assert self.config.to_address is not None
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.from_address
        message["To"] = self.config.to_address
        message.set_content(body)
        return message


def _safe_header_fragment(value: str) -> str:
    return "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in value
    )[:200]
