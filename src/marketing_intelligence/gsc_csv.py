"""Ограниченный разбор и проверка CSV Search Console без записи файла."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
import re
from typing import BinaryIO

from .link_discovery import normalize_http_url, url_origin


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DATA_ROWS = 10_000
MAX_COLUMNS = 50
MAX_URL_LENGTH = 2_048
MAX_FILENAME_LENGTH = 255
MAX_HEADER_LENGTH = 200
MAX_CELL_LENGTH = 4_096
MAX_ERROR_DETAILS = 100
ALLOWED_DELIMITERS = (",", ";", "\t")
LOGICAL_FIELDS = ("page", "clicks", "impressions", "position")
REQUIRED_FIELDS = ("page", "clicks", "impressions")
FIELD_TITLES = {
    "page": "Страница",
    "clicks": "Клики",
    "impressions": "Показы",
    "position": "Средняя позиция",
}
FIELD_SYNONYMS = {
    "page": ("Page", "Pages", "URL", "Страница", "Страницы"),
    "clicks": ("Clicks", "Клики"),
    "impressions": ("Impressions", "Показы"),
    "position": ("Position", "Average position", "Позиция", "Средняя позиция"),
}
_INTEGER_RE = re.compile(r"^[0-9]+$")


class GSCImportError(ValueError):
    """Понятная контролируемая ошибка входных данных импорта."""


@dataclass(frozen=True)
class CSVRow:
    source_line: int
    values: tuple[str, ...]


@dataclass(frozen=True)
class ParsedCSV:
    filename: str
    delimiter: str
    headers: tuple[str, ...]
    rows: tuple[CSVRow, ...]
    automatic_mapping: tuple[tuple[str, int | None], ...]

    def suggested_index(self, field: str) -> int | None:
        return dict(self.automatic_mapping).get(field)


@dataclass(frozen=True)
class ValidatedMetric:
    normalized_url: str
    clicks: int
    impressions: int
    average_position_text: str | None


@dataclass(frozen=True)
class ValidationResult:
    metrics: tuple[ValidatedMetric, ...]
    errors: tuple[str, ...]
    error_count: int


def read_limited_upload(stream: BinaryIO) -> bytes:
    """Прочитать поток не дальше лимита плюс один байт."""

    chunks: list[bytes] = []
    total = 0
    while total <= MAX_FILE_BYTES:
        chunk = stream.read(min(64 * 1024, MAX_FILE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_FILE_BYTES:
        raise GSCImportError("Файл превышает допустимый размер 5 МиБ.")
    return b"".join(chunks)


async def read_limited_async_upload(upload) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_FILE_BYTES:
        chunk = await upload.read(min(64 * 1024, MAX_FILE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_FILE_BYTES:
        raise GSCImportError("Файл превышает допустимый размер 5 МиБ.")
    return b"".join(chunks)


def parse_pages_csv(filename: str, content: bytes) -> ParsedCSV:
    raw_filename = filename.strip()
    if len(raw_filename) > MAX_FILENAME_LENGTH + 64:
        raise GSCImportError("Имя файла слишком длинное.")
    safe_filename = raw_filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if not safe_filename:
        raise GSCImportError("Выберите CSV-файл.")
    if len(safe_filename) > MAX_FILENAME_LENGTH:
        raise GSCImportError("Имя файла слишком длинное.")
    if not safe_filename.casefold().endswith(".csv"):
        raise GSCImportError("Поддерживается только файл Pages.csv в формате CSV.")
    if not content:
        raise GSCImportError("CSV-файл пуст.")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise GSCImportError("CSV должен быть сохранён в кодировке UTF-8.") from error
    if "\x00" in text:
        raise GSCImportError("CSV содержит недопустимый нулевой символ.")

    start_line, delimiter = _find_header_and_delimiter(text)
    physical_lines = text.splitlines(keepends=True)
    candidate = "".join(physical_lines[start_line - 1 :])
    try:
        reader = csv.reader(StringIO(candidate, newline=""), delimiter=delimiter, strict=True)
        header = next(reader, None)
    except csv.Error as error:
        raise GSCImportError("Не удалось разобрать структуру CSV-файла.") from error
    if header is None:
        raise GSCImportError("В CSV не найдена строка заголовков.")

    headers = tuple(value.strip().lstrip("\ufeff") for value in header)
    if not headers or all(not value for value in headers):
        raise GSCImportError("Строка заголовков CSV пуста.")
    if len(headers) > MAX_COLUMNS:
        raise GSCImportError("В CSV больше 50 столбцов.")
    if any(len(value) > MAX_HEADER_LENGTH for value in headers):
        raise GSCImportError("Название столбца CSV слишком длинное.")

    rows: list[CSVRow] = []
    try:
        for raw in reader:
            source_line = start_line - 1 + reader.line_num
            if not raw or all(not value.strip() for value in raw):
                continue
            if len(raw) != len(headers):
                raise GSCImportError(
                    f"Строка {source_line}: число значений не совпадает с заголовками."
                )
            if any(len(value) > MAX_CELL_LENGTH for value in raw):
                raise GSCImportError(f"Строка {source_line}: значение слишком длинное.")
            rows.append(CSVRow(source_line, tuple(raw)))
            if len(rows) > MAX_DATA_ROWS:
                raise GSCImportError("В CSV больше 10 000 строк данных.")
    except csv.Error as error:
        raise GSCImportError("Не удалось разобрать структуру CSV-файла.") from error
    if not rows:
        raise GSCImportError("В CSV нет строк данных.")

    return ParsedCSV(
        filename=safe_filename,
        delimiter=delimiter,
        headers=headers,
        rows=tuple(rows),
        automatic_mapping=tuple(automatic_mapping(headers).items()),
    )


def automatic_mapping(headers: tuple[str, ...]) -> dict[str, int | None]:
    normalized = [_normalize_header(value) for value in headers]
    result: dict[str, int | None] = {}
    used: set[int] = set()
    for field in LOGICAL_FIELDS:
        synonyms = {_normalize_header(value) for value in FIELD_SYNONYMS[field]}
        matches = [index for index, value in enumerate(normalized) if value in synonyms]
        result[field] = matches[0] if len(matches) == 1 and matches[0] not in used else None
        if result[field] is not None:
            used.add(result[field])
    return result


def parse_mapping(raw: dict[str, str], column_count: int) -> tuple[dict[str, int | None], dict[str, str]]:
    mapping: dict[str, int | None] = {}
    errors: dict[str, str] = {}
    for field in LOGICAL_FIELDS:
        value = raw.get(field, "").strip()
        if not value:
            mapping[field] = None
            if field in REQUIRED_FIELDS:
                errors[field] = "Выберите столбец."
            continue
        try:
            index = int(value)
        except ValueError:
            errors[field] = "Выберите существующий столбец."
            mapping[field] = None
            continue
        if index < 0 or index >= column_count:
            errors[field] = "Выберите существующий столбец."
            mapping[field] = None
            continue
        mapping[field] = index
    selected = [value for value in mapping.values() if value is not None]
    if len(selected) != len(set(selected)):
        errors["mapping"] = "Каждому полю нужен отдельный столбец."
    return mapping, errors


def validate_rows(
    parsed: ParsedCSV,
    mapping: dict[str, int | None],
    site_url: str,
) -> ValidationResult:
    normalized_site = normalize_http_url(site_url)
    if normalized_site is None:
        return ValidationResult((), ("Адрес выбранного сайта некорректен.",), 1)
    expected_origin = url_origin(normalized_site)
    metrics: list[ValidatedMetric] = []
    errors: list[str] = []
    error_count = 0
    seen: dict[str, int] = {}

    def add_error(message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_ERROR_DETAILS:
            errors.append(message)

    for row in parsed.rows:
        values = row.values
        page = _mapped_value(values, mapping.get("page")).strip()
        clicks_text = _mapped_value(values, mapping.get("clicks")).strip()
        impressions_text = _mapped_value(values, mapping.get("impressions")).strip()
        position_text = _mapped_value(values, mapping.get("position")).strip()
        row_errors_before = error_count
        if not page:
            add_error(f"Строка {row.source_line}: не указан URL страницы.")
        if not clicks_text:
            add_error(f"Строка {row.source_line}: не указаны клики.")
        if not impressions_text:
            add_error(f"Строка {row.source_line}: не указаны показы.")

        clicks = _parse_nonnegative_integer(clicks_text)
        impressions = _parse_nonnegative_integer(impressions_text)
        if clicks_text and clicks is None:
            add_error(f"Строка {row.source_line}: клики должны быть неотрицательным целым числом.")
        if impressions_text and impressions is None:
            add_error(f"Строка {row.source_line}: показы должны быть неотрицательным целым числом.")
        if clicks is not None and impressions is not None and clicks > impressions:
            add_error(f"Строка {row.source_line}: клики не могут превышать показы.")

        canonical_position: str | None = None
        if position_text:
            try:
                position = Decimal(position_text.replace(",", "."))
                if not position.is_finite() or position <= 0:
                    raise InvalidOperation
                canonical_position = format(position.normalize(), "f")
            except (InvalidOperation, ValueError):
                add_error(
                    f"Строка {row.source_line}: средняя позиция должна быть положительным конечным числом."
                )

        normalized_url = normalize_http_url(page) if page else None
        if page and (len(page) > MAX_URL_LENGTH or normalized_url is None):
            add_error(f"Строка {row.source_line}: укажите абсолютный HTTP(S)-URL без логина и пароля.")
        elif normalized_url is not None:
            if len(normalized_url) > MAX_URL_LENGTH:
                add_error(f"Строка {row.source_line}: нормализованный URL слишком длинный.")
            elif url_origin(normalized_url) != expected_origin:
                add_error(f"Строка {row.source_line}: URL принадлежит другому origin.")
            elif normalized_url in seen:
                add_error(
                    f"Строка {row.source_line}: URL совпадает после нормализации со строкой {seen[normalized_url]}."
                )
            else:
                seen[normalized_url] = row.source_line

        if error_count == row_errors_before:
            assert normalized_url is not None and clicks is not None and impressions is not None
            metrics.append(
                ValidatedMetric(normalized_url, clicks, impressions, canonical_position)
            )
    return ValidationResult(tuple(metrics), tuple(errors), error_count)


def validate_period(start_text: str, end_text: str, today: date) -> tuple[date | None, date | None, dict[str, str]]:
    errors: dict[str, str] = {}
    start = _parse_date(start_text, "period_start", errors)
    end = _parse_date(end_text, "period_end", errors)
    if start is not None and start > today:
        errors["period_start"] = "Начало периода не может быть в будущем."
    if end is not None and end > today:
        errors["period_end"] = "Конец периода не может быть в будущем."
    if start is not None and end is not None and start > end:
        errors["period_start"] = "Начало периода должно быть не позже конца."
    return start, end, errors


def _find_header_and_delimiter(text: str) -> tuple[int, str]:
    lines = text.splitlines(keepends=True)
    nonempty_indexes = [index for index, line in enumerate(lines) if line.strip()][:10]
    if not nonempty_indexes:
        raise GSCImportError("CSV-файл пуст.")
    fallback: tuple[int, str] | None = None
    for index in nonempty_indexes:
        sample = "".join(lines[index:])[:65_536]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="".join(ALLOWED_DELIMITERS))
        except csv.Error:
            continue
        if dialect.delimiter not in ALLOWED_DELIMITERS:
            continue
        if fallback is None:
            fallback = (index + 1, dialect.delimiter)
        try:
            headers = next(csv.reader([lines[index]], delimiter=dialect.delimiter, strict=True))
        except (csv.Error, StopIteration):
            continue
        suggestions = automatic_mapping(tuple(headers))
        if sum(value is not None for value in suggestions.values()) >= 2:
            return index + 1, dialect.delimiter
    if fallback is not None:
        return fallback
    raise GSCImportError("Не удалось определить допустимый разделитель CSV.")


def _normalize_header(value: str) -> str:
    return " ".join(value.lstrip("\ufeff").strip().casefold().split())


def _mapped_value(values: tuple[str, ...], index: int | None) -> str:
    if index is None or index < 0 or index >= len(values):
        return ""
    return values[index]


def _parse_nonnegative_integer(value: str) -> int | None:
    if not _INTEGER_RE.fullmatch(value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_date(value: str, field: str, errors: dict[str, str]) -> date | None:
    if not value.strip():
        errors[field] = "Укажите дату."
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors[field] = "Укажите корректную календарную дату."
        return None
