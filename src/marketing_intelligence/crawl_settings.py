"""Проверка расширенных настроек полного обхода из пользовательской формы."""

import math
import re
import unicodedata

from .crawler import CrawlSettings


MAX_PAGES_LIMIT = 200
MAX_DEPTH_LIMIT = 10
MIN_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 60.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_USER_AGENT_LENGTH = 200


def default_crawl_form() -> dict[str, str]:
    """Вернуть строковые значения формы, совпадающие с прежними настройками."""

    settings = CrawlSettings()
    return {
        "max_pages": str(settings.max_pages),
        "max_depth": str(settings.max_depth),
        "delay": _format_number(settings.delay),
        "timeout": _format_number(settings.timeout),
        "user_agent": settings.user_agent,
    }


def parse_crawl_settings(
    form: dict[str, str],
) -> tuple[CrawlSettings | None, dict[str, str]]:
    """Проверить поля формы и собрать настройки без побочных эффектов."""

    errors: dict[str, str] = {}
    max_pages = _parse_integer(
        form["max_pages"],
        minimum=1,
        maximum=MAX_PAGES_LIMIT,
        error="Введите целое число от 1 до 200.",
        field="max_pages",
        errors=errors,
    )
    max_depth = _parse_integer(
        form["max_depth"],
        minimum=0,
        maximum=MAX_DEPTH_LIMIT,
        error="Введите целое число от 0 до 10.",
        field="max_depth",
        errors=errors,
    )
    delay = _parse_decimal(
        form["delay"],
        minimum=MIN_DELAY_SECONDS,
        maximum=MAX_DELAY_SECONDS,
        error="Введите число от 0,5 до 60 секунд.",
        field="delay",
        errors=errors,
    )
    timeout = _parse_decimal(
        form["timeout"],
        minimum=MIN_TIMEOUT_SECONDS,
        maximum=MAX_TIMEOUT_SECONDS,
        error="Введите число от 1 до 120 секунд.",
        field="timeout",
        errors=errors,
    )

    user_agent = form["user_agent"].strip()
    if not user_agent:
        errors["user_agent"] = "Укажите User-Agent длиной от 1 до 200 символов."
    elif len(user_agent) > MAX_USER_AGENT_LENGTH:
        errors["user_agent"] = "User-Agent должен содержать не более 200 символов."
    elif any(unicodedata.category(character) == "Cc" for character in user_agent):
        errors["user_agent"] = (
            "User-Agent не должен содержать переносы строк или управляющие символы."
        )

    if errors:
        return None, errors

    assert max_pages is not None
    assert max_depth is not None
    assert delay is not None
    assert timeout is not None
    return (
        CrawlSettings(
            max_pages=max_pages,
            max_depth=max_depth,
            delay=delay,
            timeout=timeout,
            user_agent=user_agent,
        ),
        {},
    )


def _parse_integer(
    value: str,
    *,
    minimum: int,
    maximum: int,
    error: str,
    field: str,
    errors: dict[str, str],
) -> int | None:
    clean_value = value.strip()
    try:
        parsed = int(clean_value)
    except ValueError:
        parsed = None
    if (
        parsed is None
        or not clean_value
        or clean_value.lstrip("+-").isdigit() is False
        or parsed < minimum
        or parsed > maximum
    ):
        errors[field] = error
        return None
    return parsed


def _parse_decimal(
    value: str,
    *,
    minimum: float,
    maximum: float,
    error: str,
    field: str,
    errors: dict[str, str],
) -> float | None:
    clean_value = value.strip().replace(",", ".")
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", clean_value):
        parsed = float(clean_value)
    else:
        parsed = None
    if (
        parsed is None
        or not math.isfinite(parsed)
        or parsed < minimum
        or parsed > maximum
    ):
        errors[field] = error
        return None
    return parsed


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
