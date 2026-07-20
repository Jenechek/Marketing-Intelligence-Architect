import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from html import unescape
from pathlib import Path
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import event, inspect
from sqlmodel import Session, select
from fastapi.testclient import TestClient

from marketing_intelligence.change_event import PriceChangeEventType
from marketing_intelligence.change_event_detail import PriceValues, load_change_event
from marketing_intelligence.change_event_query import load_change_events
from marketing_intelligence.completed_crawl_processing import process_completed_crawl_run
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.config import Settings
from marketing_intelligence.crawl_history import run_crawl
from marketing_intelligence.crawler import CrawlSettings, Crawler
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (
    CrawlPagePriceRecord,
    CrawlPageRecord,
    CrawlPageSnapshot,
    CrawlRun,
    PriceChangeEvent,
    Site,
)
from marketing_intelligence.sites import add_site, delete_site
from marketing_intelligence.snapshot_comparison_input import (
    MatchedSnapshotPageVersions,
    SnapshotPageVersion,
    SnapshotPriceValue,
)
from marketing_intelligence.snapshot_price_comparison import compare_page_price


NOW = datetime(2026, 7, 20, 10, tzinfo=UTC)


def _version(identifier: int, *prices: SnapshotPriceValue) -> SnapshotPageVersion:
    return SnapshotPageVersion(
        identifier=identifier,
        url="https://shop.test/product",
        checked_at=NOW,
        title="Товар",
        description=None,
        h1="Товар",
        normalized_text="товар",
        content_hash="a" * 64,
        internal_links=(),
        prices=prices,
    )


def test_exact_price_profiles_follow_dec_028() -> None:
    price = lambda amount, currency="RUB", kind="price", source="json-ld": (
        SnapshotPriceValue(Decimal(amount), currency, kind, source)
    )
    changed = compare_page_price(
        MatchedSnapshotPageVersions(_version(1, price("10.00")), _version(2, price("10.01", source="microdata")))
    )
    unchanged_source = compare_page_price(
        MatchedSnapshotPageVersions(_version(1, price("10.00")), _version(2, price("10.00", source="microdata")))
    )
    changed_range = compare_page_price(
        MatchedSnapshotPageVersions(
            _version(1, price("10", kind="low"), price("20", kind="high")),
            _version(2, price("21", kind="high", source="microdata"), price("10", kind="low", source="microdata")),
        )
    )
    assert changed is not None and changed.current.low == Decimal("10.01")
    assert unchanged_source is None
    assert changed_range is not None and changed_range.current.high == Decimal("21")
    ambiguous = (
        (_version(1, price("10")), _version(2, price("11"), price("12"))),
        (_version(1, price("10", "RUB")), _version(2, price("11", "USD"))),
        (_version(1, price("10")), _version(2, price("-1"))),
        (_version(1, price("10", kind="low"), price("20", kind="high")), _version(2, price("30", kind="low"), price("20", kind="high"))),
        (_version(1, price("10")), _version(2, SnapshotPriceValue(None, "RUB", "price", "json-ld"))),
    )
    assert all(compare_page_price(MatchedSnapshotPageVersions(*pair)) is None for pair in ambiguous)


def _seed_pair(engine, *, site_name="shop") -> tuple[int, int, int]:
    with Session(engine) as session:
        site = Site(name=site_name, url=f"https://{site_name}.test")
        session.add(site)
        session.flush()
        runs = []
        for number in range(2):
            run = CrawlRun(
                site_id=site.id, started_at=NOW + timedelta(hours=number),
                completed_at=NOW + timedelta(hours=number), status="completed", message="ok",
                max_pages=10, max_depth=1, delay=0, timeout=5, user_agent="test",
            )
            session.add(run)
            session.flush()
            runs.append(run)
            for sequence, (slug, prices, title) in enumerate((
                ("single", (("100" if number == 0 else "120", "RUB", "price"),), "До" if number == 0 else "После"),
                ("range", (("10" if number == 0 else "11", "USD", "low"), ("20", "USD", "high")), "Диапазон"),
                ("ambiguous", (("5", "RUB", "price"),) if number == 0 else (("6", "RUB", "price"), ("7", "RUB", "price")), "Неоднозначно"),
            ), start=1):
                page = CrawlPageRecord(crawl_run_id=run.id, sequence_number=sequence, url=f"https://{site_name}.test/{slug}", depth=0, outcome="html", message="ok", http_status=200)
                session.add(page)
                session.flush()
                session.add(CrawlPageSnapshot(crawl_page_record_id=page.id, checked_at=run.completed_at, title=title, description=None, h1=title, normalized_text=title.lower(), content_hash=str(number) * 64, internal_links_json="[]"))
                for price_number, (amount, currency, kind) in enumerate(prices, start=1):
                    session.add(CrawlPagePriceRecord(crawl_page_snapshot_id=page.id, sequence_number=price_number, amount_text=amount, currency=currency, kind=kind, source="json-ld"))
        session.commit()
        return site.id, runs[0].id, runs[1].id


def test_processing_is_idempotent_query_is_unified_detail_exact_and_delete_isolated(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{(tmp_path / 'prices.db').as_posix()}")
    initialize_database(engine)
    site_id, _, current_run = _seed_pair(engine)
    other_site, _, other_current_run = _seed_pair(engine, site_name="other")
    assert process_completed_crawl_run(engine, current_run) == 5
    assert process_completed_crawl_run(engine, current_run) == 0
    assert process_completed_crawl_run(engine, other_current_run) == 5
    with Session(engine) as session:
        price_events = session.exec(
            select(PriceChangeEvent)
            .where(PriceChangeEvent.current_run_id == current_run)
            .order_by(PriceChangeEvent.url)
        ).all()
    assert [event.url.rsplit("/", 1)[-1] for event in price_events] == ["range", "single"]
    statements = 0

    def count_statement(*_args) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        page = load_change_events(engine, site_id=site_id)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert page.total_count == 5
    assert statements == 2
    assert [item.source_rank for item in page.items] == [0, 0, 0, 1, 1]
    filtered = load_change_events(engine, site_id=site_id, event_types={PriceChangeEventType.PRICE_CHANGED})
    assert filtered.total_count == 2
    single = next(item for item in filtered.items if item.url.endswith("/single"))
    detail = load_change_event(engine, site_id=site_id, event_id=single.event_id, source="price")
    assert detail is not None
    assert detail.importance is None
    assert detail.current == PriceValues("price", "RUB", "120", None)
    assert detail.previous == PriceValues("price", "RUB", "100", None)
    assert load_change_event(engine, site_id=other_site, event_id=single.event_id, source="price") is None
    assert delete_site(engine, site_id) is True
    assert load_change_events(engine, site_id=site_id).total_count == 0
    assert load_change_events(engine, site_id=other_site).total_count > 0
    engine.dispose()


def test_existing_sqlite_gets_only_missing_price_event_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    PriceChangeEvent.__table__.drop(engine)
    with Session(engine) as session:
        session.add(Site(name="Старые данные", url="https://old.test"))
        session.commit()
    assert "pricechangeevent" not in inspect(engine).get_table_names()
    initialize_database(engine)
    assert "pricechangeevent" in inspect(engine).get_table_names()
    with Session(engine) as session:
        assert session.exec(select(Site.name)).one() == "Старые данные"
    engine.dispose()


def test_price_event_ui_filter_detail_return_escaping_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "ui.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    site_id, _, current_run = _seed_pair(engine, site_name="shop<script>")
    assert process_completed_crawl_run(engine, current_run) == 5
    engine.dispose()
    before = sha256(path.read_bytes()).hexdigest()
    app = create_app(Settings(tmp_path / "data", tmp_path / "logs", f"sqlite:///{path.as_posix()}"))
    with TestClient(app) as client:
        listing = client.get(f"/sites/{site_id}/changes?event_type=price_changed&page=1")
        global_listing = client.get(f"/changes?site_id={site_id}&event_type=price_changed&page=1")
        href = re.search(r'href="([^"]*source=price[^"]*)">Подробнее</a>', listing.text)
        assert href is not None
        detail = client.get(unescape(href.group(1)))
    assert listing.status_code == global_listing.status_code == detail.status_code == 200
    assert listing.text.count("Изменение цены") >= 2
    assert "Не оценивалась" in listing.text
    assert "Нижняя цена" in detail.text or ">Цена<" in detail.text
    assert "Посадочная страница" in detail.text
    assert detail.text.index(">Стало<") < detail.text.index(">Было<")
    assert f'href="/sites/{site_id}/changes?event_type=price_changed">К событиям</a>' in detail.text
    assert "shop&lt;script&gt;" in global_listing.text
    assert "shop<script>" not in global_listing.text
    assert sha256(path.read_bytes()).hexdigest() == before


def test_actual_loopback_completed_crawls_price_range_and_ambiguity(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    version = {"value": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
            elif self.path.endswith("/range"):
                low = "10.00" if version["value"] == 1 else "11.00"
                body = (
                    '<html><head><script type="application/ld+json">'
                    '{"@type":"AggregateOffer","lowPrice":"' + low
                    + '","highPrice":"20.00","priceCurrency":"USD"}'
                    '</script></head><body>Range</body></html>'
                ).encode()
            elif self.path.endswith("/ambiguous"):
                offers = (
                    '{"@type":"Offer","price":"5","priceCurrency":"RUB"}'
                    if version["value"] == 1
                    else '[{"@type":"Offer","price":"6","priceCurrency":"RUB"},'
                    '{"@type":"Offer","price":"7","priceCurrency":"RUB"}]'
                )
                body = (
                    '<html><head><script type="application/ld+json">' + offers
                    + '</script></head><body>Ambiguous</body></html>'
                ).encode()
            else:
                price = "100.00" if version["value"] == 1 else "120.00"
                body = (
                    '<html><head><title>Product</title><script type="application/ld+json">'
                    '{"@type":"Offer","price":"' + price
                    + '","priceCurrency":"RUB"}</script></head><body>'
                    '<h1>Product</h1><a href="range">range</a>'
                    '<a href="ambiguous">ambiguous</a></body></html>'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "loopback.db"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    initialize_database(engine)
    first = add_site(engine, "Первый", f"http://127.0.0.1:{server.server_port}/one/")
    second = add_site(engine, "Второй", f"http://127.0.0.1:{server.server_port}/two/")

    async def no_wait(_seconds: float) -> None:
        return None

    async def crawl_all() -> None:
        crawler = Crawler(delay=no_wait)
        settings = CrawlSettings(max_pages=3, max_depth=1, delay=0.5, timeout=3, user_agent="Task0030Loopback/1.0")
        for site in (first, second):
            await run_crawl(engine, site.id, site.url, crawler=crawler, settings=settings)
        version["value"] = 2
        for site in (first, second):
            await run_crawl(engine, site.id, site.url, crawler=crawler, settings=settings)

    try:
        asyncio.run(crawl_all())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    for site in (first, second):
        prices = load_change_events(engine, site_id=site.id, event_types={PriceChangeEventType.PRICE_CHANGED})
        assert prices.total_count == 2
        assert {item.url.rstrip("/").rsplit("/", 1)[-1] for item in prices.items} == {
            "range", "one" if site is first else "two"
        }
    engine.dispose()
    before = sha256(path.read_bytes()).hexdigest()
    app = create_app(Settings(tmp_path / "data", tmp_path / "logs", f"sqlite:///{path.as_posix()}"))
    with TestClient(app) as client:
        common = client.get("/changes")
        filtered = client.get("/changes?site_id=1&event_type=price_changed")
        href = re.search(r'href="([^"]*source=price[^"]*)">Подробнее</a>', filtered.text)
        assert href is not None
        detail = client.get(unescape(href.group(1)))
        returned = client.get("/changes?site_id=1&event_type=price_changed")
    assert all(response.status_code == 200 for response in (common, filtered, detail, returned))
    assert "Первый" in common.text and "Второй" in common.text
    assert "Изменение цены" in filtered.text and "Не оценивалась" in filtered.text
    assert detail.text.index(">Стало<") < detail.text.index(">Было<")
    assert "Посадочная страница" in detail.text
    assert sha256(path.read_bytes()).hexdigest() == before
