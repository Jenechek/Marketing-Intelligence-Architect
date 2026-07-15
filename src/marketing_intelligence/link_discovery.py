"""Извлечение и нормализация внутренних ссылок HTML-страницы."""

from html.parser import HTMLParser
import posixpath
from urllib.parse import urljoin, urlsplit, urlunsplit


MAX_DISCOVERED_LINKS = 200


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value is not None:
                self.hrefs.append(value)
                return


def extract_internal_links(
    html: str,
    start_url: str,
    *,
    limit: int | None = MAX_DISCOVERED_LINKS,
) -> tuple[tuple[str, ...], bool]:
    """Извлечь нормализованные внутренние HTTP(S)-ссылки без перехода по ним."""

    if limit is not None and limit < 1:
        raise ValueError("Лимит ссылок должен быть положительным.")
    normalized_start = normalize_http_url(start_url)
    if normalized_start is None:
        raise ValueError("Стартовый URL должен быть корректным HTTP(S)-адресом.")
    start_origin = url_origin(normalized_start)

    parser = _HrefParser()
    parser.feed(html)
    parser.close()

    links: list[str] = []
    seen: set[str] = set()
    limited = False
    for href in parser.hrefs:
        candidate = normalize_http_url(urljoin(normalized_start, href.strip()))
        if (
            candidate is None
            or url_origin(candidate) != start_origin
            or candidate == normalized_start
            or candidate in seen
        ):
            continue
        seen.add(candidate)
        if limit is None or len(links) < limit:
            links.append(candidate)
        else:
            limited = True
            break
    return tuple(links), limited


def url_origin(url: str) -> tuple[str, str, int | None]:
    """Вернуть exact origin уже нормализованного HTTP(S)-URL."""

    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname or "", parsed.port


def normalize_http_url(url: str) -> str | None:
    """Нормализовать HTTP(S)-URL или отклонить небезопасный адрес."""

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
    except ValueError:
        return None

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = _normalize_path(parsed.path)
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    trailing_slash = path.endswith("/")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if trailing_slash and normalized != "/":
        normalized += "/"
    return normalized
