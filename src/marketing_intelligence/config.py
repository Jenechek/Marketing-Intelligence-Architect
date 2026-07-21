"""Настройки локального приложения."""

from dataclasses import dataclass, field
from datetime import tzinfo
from email.utils import parseaddr
import os
from pathlib import Path
import unicodedata


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    """Проверенная необязательная SMTP-конфигурация только из окружения."""

    host: str | None = None
    security: str = "starttls"
    port: int = 587
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    from_address: str | None = None
    to_address: str | None = None
    timeout: float = 10.0
    error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.host is not None and self.error is None

    @classmethod
    def from_environment(cls) -> "SMTPConfig":
        names = (
            "MI_SMTP_HOST",
            "MI_SMTP_SECURITY",
            "MI_SMTP_PORT",
            "MI_SMTP_USERNAME",
            "MI_SMTP_PASSWORD",
            "MI_SMTP_FROM",
            "MI_SMTP_TO",
            "MI_SMTP_TIMEOUT",
        )
        raw = {name: os.getenv(name) for name in names}
        if not any(value not in (None, "") for value in raw.values()):
            return cls()

        security = (raw["MI_SMTP_SECURITY"] or "starttls").strip().lower()
        default_port = 465 if security == "ssl" else 587
        errors: list[str] = []
        if security not in {"starttls", "ssl"}:
            errors.append("MI_SMTP_SECURITY должен быть starttls или ssl.")
        port = _parse_int(raw["MI_SMTP_PORT"], default_port, 1, 65535)
        if port is None:
            errors.append("MI_SMTP_PORT должен быть целым числом от 1 до 65535.")
            port = default_port
        timeout = _parse_float(raw["MI_SMTP_TIMEOUT"], 10.0, 1.0, 60.0)
        if timeout is None:
            errors.append("MI_SMTP_TIMEOUT должен быть числом от 1 до 60 секунд.")
            timeout = 10.0

        host = _clean(raw["MI_SMTP_HOST"])
        username = _clean(raw["MI_SMTP_USERNAME"])
        password = raw["MI_SMTP_PASSWORD"] or None
        from_address = _clean(raw["MI_SMTP_FROM"])
        to_address = _clean(raw["MI_SMTP_TO"])
        if not host:
            errors.append("Для SMTP укажите MI_SMTP_HOST.")
        if bool(username) != bool(password):
            errors.append("MI_SMTP_USERNAME и MI_SMTP_PASSWORD задаются только вместе.")
        for label, address in (
            ("MI_SMTP_FROM", from_address),
            ("MI_SMTP_TO", to_address),
        ):
            if not _valid_single_address(address):
                errors.append(f"{label} должен содержать один корректный адрес.")
        for label, value in (
            ("MI_SMTP_HOST", host),
            ("MI_SMTP_USERNAME", username),
            ("MI_SMTP_FROM", from_address),
            ("MI_SMTP_TO", to_address),
        ):
            if value and _has_control(value):
                errors.append(f"{label} не должен содержать управляющие символы.")

        return cls(
            host=host,
            security=security,
            port=port,
            username=username,
            password=password,
            from_address=from_address,
            to_address=to_address,
            timeout=timeout,
            error=" ".join(errors) or None,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """Пути, которые можно заменить для тестов или отдельной установки."""

    data_dir: Path
    logs_dir: Path
    database_url: str
    local_timezone: tzinfo | None = None
    smtp: SMTPConfig = field(default_factory=SMTPConfig.from_environment)

    @classmethod
    def from_environment(cls) -> "Settings":
        data_dir = Path(os.getenv("MI_DATA_DIR", "data"))
        logs_dir = Path(os.getenv("MI_LOGS_DIR", "logs"))
        database_url = os.getenv("MI_DATABASE_URL")

        if database_url is None:
            database_path = (data_dir / "marketing_intelligence.db").resolve()
            database_url = f"sqlite:///{database_path.as_posix()}"

        return cls(
            data_dir=data_dir,
            logs_dir=logs_dir,
            database_url=database_url,
        )


def _clean(value: str | None) -> str | None:
    clean = (value or "").strip()
    return clean or None


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _valid_single_address(value: str | None) -> bool:
    if not value or "," in value or ";" in value or _has_control(value):
        return False
    display, address = parseaddr(value)
    return not display and address == value and "@" in address


def _parse_int(
    value: str | None, default: int, minimum: int, maximum: int
) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if minimum <= parsed <= maximum else None


def _parse_float(
    value: str | None, default: float, minimum: float, maximum: float
) -> float | None:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if minimum <= parsed <= maximum else None
