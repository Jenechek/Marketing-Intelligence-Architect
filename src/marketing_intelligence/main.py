"""Точка сборки минимального FastAPI-приложения."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
import secrets
from typing import Callable
from urllib.parse import parse_qs

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .availability import AvailabilityChecker, AvailabilityResult, status_title
from .check_history import (
    complete_check,
    count_checks,
    format_check_count,
    list_checks,
    start_check,
    to_local_datetime,
)
from .change_event_detail import ChangeEventDataError, load_change_event
from .change_event_export import prepare_change_event_export
from .change_event import HISTORY_EVENT_TYPES
from .change_event_filters import (
    EVENTS_PER_PAGE,
    ChangeEventListForm,
    change_event_list_url,
    global_change_event_list_url,
    parse_change_event_list_state,
)
from .change_event_presentation import (
    event_explanation,
    event_type_title,
    importance_title,
    present_sides,
)
from .change_event_query import has_change_events, load_change_events
from .change_event_view_state import set_change_event_viewed
from .confirmation import (
    CHECK_AVAILABILITY_ACTION,
    SAVE_SCHEDULE_ACTION,
    START_CRAWL_ACTION,
    TEST_SMTP_ACTION,
    change_event_view_action,
    create_action_token,
    create_delete_confirmation_token,
    validate_delete_confirmation_token,
    validate_action_token,
    retry_scheduled_crawl_action,
)
from .config import Settings
from .database import build_engine, initialize_database
from .gsc_csv import (
    FIELD_TITLES,
    GSCImportError,
    LOGICAL_FIELDS,
    parse_mapping,
    parse_pages_csv,
    read_limited_async_upload,
    validate_period,
    validate_rows,
)
from .gsc_persistence import save_import
from .gsc_preview import ImportPreview, PreviewStore
from .gsc_query import count_import_data, list_imports, list_metrics, list_periods
from .crawl_history import (
    ActiveCrawlRunError,
    RUNNING_STATUS,
    count_crawl_data,
    crawl_error_outcome_title,
    crawl_status_title,
    execute_crawl_run,
    get_crawl_run,
    get_running_crawl_run,
    list_crawl_errors,
    recover_interrupted_runs,
    start_crawl_run,
)
from .crawl_settings import default_crawl_form, parse_crawl_settings
from .crawler import CrawlSettings, Crawler
from .crawl_dispatcher import CrawlQueueDispatcher
from .logging_config import configure_logging
from .models import Site, SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED
from .scheduler import (
    FREQUENCY_TITLES,
    RETRYABLE,
    WEEKDAY_TITLES,
    count_schedule_data,
    create_retry,
    default_schedule_form,
    get_schedule,
    list_entries,
    load_schedule_summaries,
    parse_schedule_form,
    notification_status_title,
    reconcile_missed_schedules,
    recover_interrupted_entries,
    save_schedule,
    schedule_to_form,
    status_title as scheduled_status_title,
)
from .site_structure import (
    OUTCOME_TITLES,
    SIGNAL_TITLES,
    StructuralSignal,
    StructureDataError,
)
from .site_structure_filters import (
    PAGES_PER_PAGE,
    StructureFilterState,
    filter_structure_pages,
    parse_structure_filters,
    structure_url,
)
from .site_structure_presentation import build_graph_view, safe_external_url
from .site_structure_query import has_site_structure, load_site_structure
from .sites import (
    ActiveSiteCrawlError,
    SiteTransferBlockedError,
    add_site,
    delete_site,
    get_site,
    get_site_of_type,
    list_sites,
    transfer_site,
    update_site,
    validate_site,
)
from .smtp_notifications import MailTransport, SMTPNotifier


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
UPLOAD_GSC_ACTION = "upload-gsc-pages"

SITE_SECTIONS = {
    SITE_TYPE_COMPETITOR: {
        "path": "/competitors",
        "title": "Конкуренты",
        "eyebrow": "Мониторинг конкурентов",
        "lead": "Добавляйте сайты конкурентов и отслеживайте их изменения.",
        "item_label": "сайт конкурента",
        "add_title": "Добавить сайт конкурента",
        "empty_title": "Сайтов пока нет",
        "history_path": "/competitors/changes",
        "history_title": "История изменений конкурентов",
    },
    SITE_TYPE_OWNED: {
        "path": "/own-sites",
        "title": "Свои сайты",
        "eyebrow": "Собственные сайты",
        "lead": "Следите за своими сайтами и загружайте показатели Search Console.",
        "item_label": "собственный сайт",
        "add_title": "Добавить собственный сайт",
        "empty_title": "Своих сайтов пока нет",
        "history_path": "/own-sites/changes",
        "history_title": "История изменений собственных сайтов",
    },
}


def site_list_url(site: Site) -> str:
    return SITE_SECTIONS[site.site_type]["path"]


def transfer_action(source_type: str, target_type: str) -> str:
    return f"transfer-site:{source_type}:{target_type}"


templates.env.globals["site_list_url"] = site_list_url
templates.env.globals["site_section"] = lambda site: SITE_SECTIONS[site.site_type]


def confirm_gsc_action(preview_token: str) -> str:
    return f"confirm-gsc-pages:{preview_token}"


def create_app(
    settings: Settings | None = None,
    *,
    availability_checker: AvailabilityChecker | None = None,
    crawler: Crawler | None = None,
    smtp_transport: MailTransport | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Создать приложение с переданными или локальными настройками."""

    active_settings = settings or Settings.from_environment()
    clock = now_provider or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_settings.data_dir.mkdir(parents=True, exist_ok=True)
        active_settings.logs_dir.mkdir(parents=True, exist_ok=True)

        logger = configure_logging(active_settings.logs_dir)
        engine = build_engine(active_settings.database_url)
        initialize_database(engine)
        recover_interrupted_runs(engine)
        recover_interrupted_entries(engine, now=clock())
        reconcile_missed_schedules(
            engine,
            now=clock(),
            local_timezone=active_settings.local_timezone,
        )

        application.state.settings = active_settings
        application.state.engine = engine
        application.state.logger = logger
        application.state.action_token_secret = secrets.token_bytes(32)
        application.state.gsc_previews = PreviewStore(now_provider=clock)
        application.state.availability_checker = availability_checker or AvailabilityChecker()
        application.state.crawler = crawler or Crawler()
        application.state.smtp_notifier = SMTPNotifier(
            active_settings.smtp,
            transport=smtp_transport,
        )
        application.state.crawl_dispatcher = CrawlQueueDispatcher(
            engine,
            application.state.crawler,
            application.state.smtp_notifier,
            logger,
            local_timezone=active_settings.local_timezone,
        )
        await application.state.crawl_dispatcher.send_pending_notifications()
        scheduler = AsyncIOScheduler(timezone=UTC)

        async def scheduler_tick() -> None:
            await application.state.crawl_dispatcher.wake(now=clock())

        scheduler.add_job(
            scheduler_tick,
            "interval",
            seconds=1,
            coalesce=True,
            max_instances=1,
            id="crawl-queue-wakeup",
        )
        scheduler.start()
        application.state.scheduler = scheduler
        application.state.crawl_tasks = set()
        logger.info("Marketing Intelligence запущен")

        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await application.state.crawl_dispatcher.shutdown()
            tasks = tuple(application.state.crawl_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            engine.dispose()
            logger.info("Marketing Intelligence остановлен")

    application = FastAPI(
        title="Marketing Intelligence",
        description="Локальное приложение для маркетинговой аналитики",
        lifespan=lifespan,
    )
    application.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIR / "static"),
        name="static",
    )

    def render_sites(
        request: Request,
        site_type: str,
        *,
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        section = SITE_SECTIONS[site_type]
        sites = list_sites(request.app.state.engine, site_type)
        summaries = load_schedule_summaries(
            request.app.state.engine,
            [site.id for site in sites if site.id is not None],
        )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "sites": sites,
                "site_type": site_type,
                "section": section,
                "sections": SITE_SECTIONS,
                "schedule_summaries": summaries,
                "scheduled_status_title": scheduled_status_title,
                "to_local_datetime": to_local_datetime,
                "form": form or {"name": "", "url": ""},
                "errors": errors or {},
                "created": request.query_params.get("created") == "1",
                "updated": request.query_params.get("updated") == "1",
                "deleted": request.query_params.get("deleted") == "1",
                "transferred": request.query_params.get("transferred") == "1",
            },
            status_code=status_code,
        )

    @application.get("/")
    async def home() -> RedirectResponse:
        return RedirectResponse(url="/competitors", status_code=307)

    @application.get("/competitors", response_class=HTMLResponse)
    async def competitors(request: Request) -> HTMLResponse:
        return render_sites(request, SITE_TYPE_COMPETITOR)

    @application.get("/own-sites", response_class=HTMLResponse)
    async def own_sites(request: Request) -> HTMLResponse:
        return render_sites(request, SITE_TYPE_OWNED)

    async def create_site_in_section(
        request: Request,
        site_type: str,
    ) -> HTMLResponse:
        raw_form = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        form = {
            "name": raw_form.get("name", [""])[0],
            "url": raw_form.get("url", [""])[0],
        }
        errors = validate_site(form["name"], form["url"])
        if errors:
            return render_sites(
                request,
                site_type,
                form=form,
                errors=errors,
                status_code=422,
            )

        add_site(request.app.state.engine, form["name"], form["url"], site_type)
        return RedirectResponse(
            url=f"{SITE_SECTIONS[site_type]['path']}?created=1",
            status_code=303,
        )

    @application.post("/competitors", response_class=HTMLResponse)
    async def create_competitor(request: Request) -> HTMLResponse:
        return await create_site_in_section(request, SITE_TYPE_COMPETITOR)

    @application.post("/own-sites", response_class=HTMLResponse)
    async def create_owned_site(request: Request) -> HTMLResponse:
        return await create_site_in_section(request, SITE_TYPE_OWNED)

    @application.post("/sites", response_class=HTMLResponse)
    async def create_legacy_site(request: Request) -> HTMLResponse:
        return await create_site_in_section(request, SITE_TYPE_COMPETITOR)

    @application.get("/sites/{site_id}/imports")
    async def legacy_gsc_import_screen(request: Request, site_id: int):
        site = get_site_of_type(
            request.app.state.engine, site_id, SITE_TYPE_OWNED
        )
        if site is None:
            return render_site_not_found(request)
        query = str(request.query_params)
        target = f"/own-sites/{site_id}/imports"
        return RedirectResponse(
            url=target + (f"?{query}" if query else ""),
            status_code=307,
        )

    @application.get("/own-sites/{site_id}/imports", response_class=HTMLResponse)
    async def gsc_import_screen(request: Request, site_id: int) -> HTMLResponse:
        site = get_site_of_type(
            request.app.state.engine, site_id, SITE_TYPE_OWNED
        )
        if site is None:
            return render_site_not_found(request)
        page, page_error = parse_positive_page(request.query_params.get("page", "1"))
        return render_import_screen(
            request,
            site,
            page=page,
            action_error=page_error,
            status_code=422 if page_error else 200,
        )

    @application.post("/own-sites/{site_id}/imports/preview", response_class=HTMLResponse)
    async def gsc_import_preview(request: Request, site_id: int) -> HTMLResponse:
        site = get_site_of_type(
            request.app.state.engine, site_id, SITE_TYPE_OWNED
        )
        if site is None:
            return render_site_not_found(request)
        try:
            raw = await request.form()
        except Exception:
            return render_import_screen(
                request,
                site,
                action_error="Не удалось прочитать multipart-форму загрузки.",
                status_code=422,
            )
        action_token = str(raw.get("action_token", ""))
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            UPLOAD_GSC_ACTION,
            action_token,
        ):
            return render_import_error(
                request,
                site,
                title="Загрузка не разрешена",
                message="Защитный токен отсутствует или недействителен. Откройте экран импорта заново.",
                status_code=403,
            )
        form = {
            "period_start": str(raw.get("period_start", "")),
            "period_end": str(raw.get("period_end", "")),
            "report_confirmed": str(raw.get("report_confirmed", "")),
        }
        today = clock().astimezone(active_settings.local_timezone).date()
        period_start, period_end, errors = validate_period(
            form["period_start"], form["period_end"], today
        )
        if form["report_confirmed"] != "yes":
            errors["report_confirmed"] = (
                "Подтвердите, что это вкладка «Страницы» без дополнительных фильтров."
            )
        upload = raw.get("csv_file")
        parsed = None
        if not isinstance(upload, UploadFile) and not (
            hasattr(upload, "read") and hasattr(upload, "filename")
        ):
            upload = None
        if upload is None or not upload.filename:
            errors["csv_file"] = "Выберите CSV-файл."
        else:
            try:
                content = await read_limited_async_upload(upload)
                parsed = parse_pages_csv(upload.filename, content)
            except GSCImportError as error:
                errors["csv_file"] = str(error)
            finally:
                await upload.close()
        if errors or parsed is None or period_start is None or period_end is None:
            return render_import_screen(
                request, site, form=form, errors=errors, status_code=422
            )
        preview = request.app.state.gsc_previews.add(
            site_id, period_start, period_end, parsed
        )
        return render_mapping_screen(request, site, preview)

    @application.post("/own-sites/{site_id}/imports/confirm", response_class=HTMLResponse)
    async def confirm_gsc_import(request: Request, site_id: int) -> HTMLResponse:
        site = get_site_of_type(
            request.app.state.engine, site_id, SITE_TYPE_OWNED
        )
        if site is None:
            return render_site_not_found(request)
        body = await read_small_form_body(request)
        if body is None:
            return render_import_error(
                request,
                site,
                title="Подтверждение слишком велико",
                message="Повторите загрузку и выберите столбцы заново.",
                status_code=422,
            )
        raw = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        value = lambda name: raw.get(name, [""])[0]
        preview_token = value("preview_token")
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            confirm_gsc_action(preview_token),
            value("action_token"),
        ):
            return render_import_error(
                request,
                site,
                title="Импорт не разрешён",
                message="Защитный токен отсутствует или недействителен. Повторите загрузку файла.",
                status_code=403,
            )
        preview = request.app.state.gsc_previews.get(preview_token, site_id)
        if preview is None:
            return render_import_error(
                request,
                site,
                title="Предпросмотр недоступен",
                message="Он истёк, уже использован, принадлежит другому сайту или приложение было перезапущено. Загрузите файл снова.",
                status_code=409,
            )
        mapping_form = {field: value(field) for field in LOGICAL_FIELDS}
        mapping, mapping_errors = parse_mapping(
            mapping_form, len(preview.parsed.headers)
        )
        if mapping_errors:
            return render_mapping_screen(
                request,
                site,
                preview,
                mapping_form=mapping_form,
                errors=mapping_errors,
                status_code=422,
            )
        acquired = request.app.state.gsc_previews.acquire(preview_token, site_id)
        if acquired is None:
            return render_import_error(
                request,
                site,
                title="Импорт уже подтверждается",
                message="Повторная отправка не создаст второй импорт. Обновите историю через несколько секунд.",
                status_code=409,
            )
        validation = validate_rows(acquired.parsed, mapping, site.url)
        if validation.error_count:
            request.app.state.gsc_previews.release(preview_token)
            return render_mapping_screen(
                request,
                site,
                acquired,
                mapping_form=mapping_form,
                row_errors=validation.errors,
                row_error_count=validation.error_count,
                status_code=422,
            )
        try:
            save_import(
                request.app.state.engine,
                site_id=site_id,
                filename=acquired.parsed.filename,
                period_start=acquired.period_start,
                period_end=acquired.period_end,
                delimiter=acquired.parsed.delimiter,
                metrics=validation.metrics,
                now_provider=clock,
            )
        except Exception as error:
            request.app.state.gsc_previews.release(preview_token)
            request.app.state.logger.error("Импорт Search Console не сохранён: %s", error)
            return render_import_error(
                request,
                site,
                title="Импорт не сохранён",
                message="Транзакция отменена, ранее сохранённые данные не изменены. Повторите подтверждение.",
                status_code=500,
            )
        request.app.state.gsc_previews.consume(preview_token)
        return RedirectResponse(
            url=f"/own-sites/{site_id}/imports?imported=1",
            status_code=303,
        )

    @application.get("/sites/{site_id}/gsc-pages")
    async def legacy_gsc_page_metrics(request: Request, site_id: int):
        site = get_site_of_type(
            request.app.state.engine, site_id, SITE_TYPE_OWNED
        )
        if site is None:
            return render_site_not_found(request)
        query = str(request.query_params)
        target = f"/own-sites/{site_id}/gsc-pages"
        return RedirectResponse(
            url=target + (f"?{query}" if query else ""),
            status_code=307,
        )

    @application.get("/own-sites/{site_id}/gsc-pages", response_class=HTMLResponse)
    async def gsc_page_metrics(request: Request, site_id: int) -> HTMLResponse:
        site = get_site_of_type(
            request.app.state.engine, site_id, SITE_TYPE_OWNED
        )
        if site is None:
            return render_site_not_found(request)
        periods = list_periods(request.app.state.engine, site_id)
        errors: dict[str, str] = {}
        combined_period = request.query_params.get("period", "")
        start_text = request.query_params.get("period_start", "")
        end_text = request.query_params.get("period_end", "")
        if combined_period:
            try:
                start_text, end_text = combined_period.split("|", maxsplit=1)
            except ValueError:
                start_text = end_text = "invalid"
        if start_text or end_text:
            try:
                selected = (date.fromisoformat(start_text), date.fromisoformat(end_text))
            except ValueError:
                selected = None
                errors["period"] = "Выберите существующий период."
            if selected is not None and selected not in periods:
                errors["period"] = "Выберите существующий период."
                selected = None
        else:
            selected = periods[0] if periods else None
        page, page_error = parse_positive_page(request.query_params.get("page", "1"))
        if page_error:
            errors["page"] = page_error
        metric_page = None
        if selected is not None:
            metric_page = list_metrics(
                request.app.state.engine, site_id, selected[0], selected[1], page
            )
            if page > metric_page.total_pages and metric_page.total_items:
                errors["page"] = "Номер страницы выходит за пределы показателей."
                page = metric_page.total_pages
                metric_page = list_metrics(
                    request.app.state.engine, site_id, selected[0], selected[1], page
                )
        return templates.TemplateResponse(
            request=request,
            name="gsc_metrics.html",
            context={
                "site": site,
                "periods": periods,
                "selected": selected,
                "metric_page": metric_page,
                "errors": errors,
                "format_ctr": lambda ctr: f"{(ctr * Decimal(100)):.2f} %",
            },
            status_code=422 if errors else 200,
        )

    def render_edit_site(
        request: Request,
        site: Site,
        *,
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="edit_site.html",
            context={
                "site": site,
                "form": form or {"name": site.name, "url": site.url},
                "errors": errors or {},
            },
            status_code=status_code,
        )

    def render_site_not_found(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="site_not_found.html",
            status_code=404,
        )

    def render_change_event_error(
        request: Request,
        site_id: int | None,
        *,
        title: str,
        message: str,
        status_code: int,
        return_url: str | None = None,
        action_label: str = "Вернуться к событиям",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="change_event_error.html",
            context={
                "title": title,
                "message": message,
                "return_url": return_url or (
                    f"/sites/{site_id}/changes" if site_id is not None else "/changes"
                ),
                "action_label": action_label,
            },
            status_code=status_code,
        )

    def to_event_local_datetime(value):
        return to_local_datetime(
            value,
            target_timezone=active_settings.local_timezone,
        )

    def view_state_token(
        request: Request,
        site_id: int,
        source: str,
        event_id: int,
        viewed: bool,
    ) -> str:
        return create_action_token(
            request.app.state.action_token_secret,
            site_id,
            change_event_view_action(source, event_id, viewed),
        )

    def prepare_export_response(
        request: Request,
        format_name: str,
        state,
        *,
        site_id: int | None = None,
        site_type: str | None = None,
    ):
        try:
            prepared = prepare_change_event_export(
                request.app.state.engine,
                format_name=format_name,
                site_id=state.site_id if site_id is None else site_id,
                site_type=site_type,
                event_types=(state.event_type,) if state.event_type else None,
                from_time=state.from_time,
                before_time=state.before_time,
                viewed=state.viewed,
                local_timezone=active_settings.local_timezone,
            )
        except (ChangeEventDataError, ValueError) as error:
            request.app.state.logger.error("Экспорт истории не подготовлен: %s", error)
            return render_change_event_error(
                request,
                state.site_id if site_id is None else site_id,
                title="Экспорт нельзя подготовить",
                message=(
                    "Файл не создан: часть связанных данных повреждена. "
                    "Сохранённая история не изменена."
                ),
                status_code=500,
            )
        return StreamingResponse(
            prepared.chunks(),
            media_type=prepared.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{prepared.filename}"',
            },
        )

    def render_delete_forbidden(request: Request, site: Site) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="delete_forbidden.html",
            context={"site": site},
            status_code=403,
        )

    def render_check_site(
        request: Request,
        site: Site,
        *,
        result: AvailabilityResult | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="check_site.html",
            context={
                "site": site,
                "result": result,
                "history": list_checks(request.app.state.engine, site.id),
                "status_title": status_title,
                "to_local_datetime": to_local_datetime,
                "action_token": create_action_token(
                    request.app.state.action_token_secret,
                    site.id,
                    CHECK_AVAILABILITY_ACTION,
                ),
            },
            status_code=status_code,
        )

    def render_check_forbidden(request: Request, site: Site) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="check_forbidden.html",
            context={"site": site},
            status_code=403,
        )

    def render_delete_running(
        request: Request,
        site: Site,
        run_id: int,
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="delete_running.html",
            context={"site": site, "run_id": run_id},
            status_code=status_code,
        )

    def render_crawl_screen(
        request: Request,
        site: Site,
        *,
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="crawl_site.html",
            context={
                "site": site,
                "form": form or default_crawl_form(),
                "errors": errors or {},
                "action_token": create_action_token(
                    request.app.state.action_token_secret,
                    site.id,
                    START_CRAWL_ACTION,
                ),
            },
            status_code=status_code,
        )

    def render_crawl_forbidden(request: Request, site: Site) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="crawl_forbidden.html",
            context={"site": site},
            status_code=403,
        )

    def render_crawl_not_found(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="crawl_not_found.html",
            status_code=404,
        )

    def render_schedule_screen(
        request: Request,
        site: Site,
        *,
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
        action_error: str | None = None,
    ) -> HTMLResponse:
        schedule = get_schedule(request.app.state.engine, site.id)
        raw_page = request.query_params.get("page", "1")
        try:
            page_number = int(raw_page)
            if str(page_number) != raw_page or not 1 <= page_number <= 1_000_000:
                raise ValueError
        except ValueError:
            page_number = 1
            action_error = "Номер страницы должен быть положительным целым числом."
            status_code = 422
        history = list_entries(request.app.state.engine, site.id, page_number)
        smtp = active_settings.smtp
        smtp_state = (
            "Настроен"
            if smtp.enabled
            else (f"Ошибка настройки: {smtp.error}" if smtp.error else "Выключен")
        )
        retry_tokens = {
            entry.id: create_action_token(
                request.app.state.action_token_secret,
                site.id,
                retry_scheduled_crawl_action(entry.id),
            )
            for entry in history.entries
            if entry.id is not None and entry.status in RETRYABLE
        }
        return templates.TemplateResponse(
            request=request,
            name="schedule.html",
            context={
                "site": site,
                "schedule": schedule,
                "form": form
                or (
                    schedule_to_form(schedule)
                    if schedule is not None
                    else default_schedule_form(
                        local_timezone=active_settings.local_timezone,
                        now=clock(),
                    )
                ),
                "errors": errors or {},
                "action_error": action_error,
                "history": history,
                "frequency_titles": FREQUENCY_TITLES,
                "weekday_titles": WEEKDAY_TITLES,
                "status_title": scheduled_status_title,
                "notification_status_title": notification_status_title,
                "retry_tokens": retry_tokens,
                "to_local_datetime": to_event_local_datetime,
                "smtp_state": smtp_state,
                "smtp_enabled": smtp.enabled,
                "save_token": create_action_token(
                    request.app.state.action_token_secret,
                    site.id,
                    SAVE_SCHEDULE_ACTION,
                ),
                "smtp_test_token": create_action_token(
                    request.app.state.action_token_secret,
                    site.id,
                    TEST_SMTP_ACTION,
                ),
                "saved": request.query_params.get("saved") == "1",
                "retried": request.query_params.get("retried") == "1",
                "email_test": request.query_params.get("email_test"),
            },
            status_code=status_code,
        )

    def parse_positive_page(value: str) -> tuple[int, str | None]:
        try:
            page = int(value)
            if str(page) != value or not 1 <= page <= 1_000_000:
                raise ValueError
        except ValueError:
            return 1, "Номер страницы должен быть положительным целым числом."
        return page, None

    async def read_small_form_body(request: Request, limit: int = 65_536) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def render_import_screen(
        request: Request,
        site: Site,
        *,
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
        page: int = 1,
        action_error: str | None = None,
    ) -> HTMLResponse:
        history = list_imports(request.app.state.engine, site.id, page)
        if page > history.total_pages and history.total_items:
            page = history.total_pages
            history = list_imports(request.app.state.engine, site.id, page)
            action_error = "Номер страницы выходит за пределы истории."
            status_code = 422
        return templates.TemplateResponse(
            request=request,
            name="gsc_import.html",
            context={
                "site": site,
                "form": form or {"period_start": "", "period_end": "", "report_confirmed": ""},
                "errors": errors or {},
                "action_error": action_error,
                "history": history,
                "imported": request.query_params.get("imported") == "1",
                "upload_token": create_action_token(
                    request.app.state.action_token_secret, site.id, UPLOAD_GSC_ACTION
                ),
                "to_local_datetime": to_event_local_datetime,
            },
            status_code=status_code,
        )

    def render_mapping_screen(
        request: Request,
        site: Site,
        preview: ImportPreview,
        *,
        mapping_form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        row_errors: tuple[str, ...] = (),
        row_error_count: int = 0,
        status_code: int = 200,
    ) -> HTMLResponse:
        if mapping_form is None:
            mapping_form = {
                field: (
                    "" if preview.parsed.suggested_index(field) is None
                    else str(preview.parsed.suggested_index(field))
                )
                for field in LOGICAL_FIELDS
            }
        return templates.TemplateResponse(
            request=request,
            name="gsc_mapping.html",
            context={
                "site": site,
                "preview": preview,
                "mapping_form": mapping_form,
                "errors": errors or {},
                "row_errors": row_errors,
                "row_error_count": row_error_count,
                "field_titles": FIELD_TITLES,
                "confirm_token": create_action_token(
                    request.app.state.action_token_secret,
                    site.id,
                    confirm_gsc_action(preview.token),
                ),
            },
            status_code=status_code,
        )

    def render_import_error(
        request: Request,
        site: Site,
        *,
        title: str,
        message: str,
        status_code: int,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="gsc_import_error.html",
            context={"site": site, "title": title, "message": message},
            status_code=status_code,
        )

    def render_structure_error(
        request: Request,
        site: Site,
        *,
        title: str,
        message: str,
        status_code: int,
        return_url: str | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="site_structure_error.html",
            context={
                "site": site,
                "title": title,
                "message": message,
                "return_url": return_url or f"/sites/{site.id}/structure",
            },
            status_code=status_code,
        )

    async def perform_crawl(run_id: int, site: Site, settings: CrawlSettings) -> None:
        try:
            await execute_crawl_run(
                application.state.engine,
                run_id,
                site.url,
                crawler=application.state.crawler,
                settings=settings,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            application.state.logger.exception(
                "Фоновая обработка запуска обхода завершилась ошибкой (run_id=%s)",
                run_id,
            )

    def render_transfer_site(
        request: Request,
        site: Site,
        *,
        message: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        target_type = (
            SITE_TYPE_OWNED
            if site.site_type == SITE_TYPE_COMPETITOR
            else SITE_TYPE_COMPETITOR
        )
        gsc_import_count, gsc_metric_count = count_import_data(
            request.app.state.engine, site.id
        )
        return templates.TemplateResponse(
            request=request,
            name="transfer_site.html",
            context={
                "site": site,
                "source_type": site.site_type,
                "target_type": target_type,
                "source_section": SITE_SECTIONS[site.site_type],
                "target_section": SITE_SECTIONS[target_type],
                "gsc_import_count": gsc_import_count,
                "gsc_metric_count": gsc_metric_count,
                "message": message,
                "action_token": create_action_token(
                    request.app.state.action_token_secret,
                    site.id,
                    transfer_action(site.site_type, target_type),
                ),
            },
            status_code=status_code,
        )

    @application.get("/sites/{site_id}/transfer", response_class=HTMLResponse)
    async def confirm_transfer_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        return render_transfer_site(request, site)

    @application.post("/sites/{site_id}/transfer", response_class=HTMLResponse)
    async def move_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        body = await read_small_form_body(request)
        if body is None:
            return render_transfer_site(
                request,
                site,
                message="Данные подтверждения слишком велики. Обновите страницу.",
                status_code=413,
            )
        raw = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        value = lambda name: raw.get(name, [""])[0]
        source_type = value("source_type")
        target_type = value("target_type")
        expected_target = (
            SITE_TYPE_OWNED
            if site.site_type == SITE_TYPE_COMPETITOR
            else SITE_TYPE_COMPETITOR
        )
        if source_type != site.site_type or target_type != expected_target:
            return render_transfer_site(
                request,
                site,
                message="Направление переноса устарело или указано неверно.",
                status_code=409,
            )
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            transfer_action(source_type, target_type),
            value("action_token"),
        ):
            return render_transfer_site(
                request,
                site,
                message="Защитный токен недействителен. Обновите страницу.",
                status_code=403,
            )
        try:
            moved = transfer_site(
                request.app.state.engine,
                site_id,
                source_type,
                target_type,
            )
        except SiteTransferBlockedError as error:
            return render_transfer_site(
                request,
                site,
                message=str(error),
                status_code=409,
            )
        if moved is None:
            current = get_site(request.app.state.engine, site_id)
            if current is None:
                return render_site_not_found(request)
            return render_transfer_site(
                request,
                current,
                message="Сайт уже перенесён или подтверждение устарело.",
                status_code=409,
            )
        return RedirectResponse(
            url=f"{site_list_url(moved)}?transferred=1",
            status_code=303,
        )

    @application.get("/sites/{site_id}/edit", response_class=HTMLResponse)
    async def edit_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        return render_edit_site(request, site)

    @application.get("/sites/{site_id}/changes", response_class=HTMLResponse)
    async def change_events_screen(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        form = ChangeEventListForm(
            site_id="",
            event_type=request.query_params.get("event_type", ""),
            date_from=request.query_params.get("date_from", ""),
            date_to=request.query_params.get("date_to", ""),
            page=request.query_params.get("page", "1"),
            view_status=request.query_params.get("view_status", ""),
        )
        state, errors = parse_change_event_list_state(
            event_type=form.event_type,
            date_from=form.date_from,
            date_to=form.date_to,
            page=form.page,
            view_status=form.view_status,
            local_timezone=active_settings.local_timezone,
        )
        if state is None:
            return templates.TemplateResponse(
                request=request,
                name="change_events.html",
                context={
                    "site": site,
                    "event_page": None,
                    "form": form,
                    "errors": errors,
                    "event_types": HISTORY_EVENT_TYPES,
                    "event_type_title": event_type_title,
                },
                status_code=422,
            )
        try:
            offset = (state.page - 1) * EVENTS_PER_PAGE
            query_offset = offset if offset <= (1 << 63) - 1 else 0
            event_page = load_change_events(
                request.app.state.engine,
                site_id=site_id,
                event_types=(state.event_type,) if state.event_type else None,
                from_time=state.from_time,
                before_time=state.before_time,
                viewed=state.viewed,
                limit=EVENTS_PER_PAGE,
                offset=query_offset,
            )
            if state.page > 1 and offset >= event_page.total_count:
                return render_change_event_error(
                    request,
                    site_id,
                    title="Страница событий не найдена",
                    message="Запрошенной страницы нет. Перейдите к первой странице результатов.",
                    status_code=404,
                    return_url=change_event_list_url(site_id, state, page=1),
                    action_label="К первой странице",
                )
            has_any_history = bool(event_page.total_count)
            if not has_any_history and state.has_filters:
                has_any_history = has_change_events(
                    request.app.state.engine,
                    site_id=site_id,
                )
        except ValueError as error:
            request.app.state.logger.error(
                "Повреждён список событий сайта %s: %s",
                site_id,
                error,
            )
            return render_change_event_error(
                request,
                site_id,
                title="События нельзя показать",
                message=(
                    "Часть сохранённых данных повреждена. "
                    "Сохранённая история не изменена."
                ),
                status_code=500,
            )
        return templates.TemplateResponse(
            request=request,
            name="change_events.html",
            context={
                "site": site,
                "event_page": event_page,
                "form": form,
                "errors": {},
                "state": state,
                "event_types": HISTORY_EVENT_TYPES,
                "has_any_history": has_any_history,
                "current_page": state.page,
                "total_pages": max(
                    1,
                    (event_page.total_count + EVENTS_PER_PAGE - 1)
                    // EVENTS_PER_PAGE,
                ),
                "previous_url": (
                    change_event_list_url(site_id, state, page=state.page - 1)
                    if state.page > 1
                    else None
                ),
                "next_url": (
                    change_event_list_url(site_id, state, page=state.page + 1)
                    if state.page * EVENTS_PER_PAGE < event_page.total_count
                    else None
                ),
                "detail_url": lambda event: (
                    f"/sites/{site_id}/changes/{event.event_id}"
                    + ("?" + "&".join(filter(None, [
                        "source=price" if event.source == "price" else "",
                        state.query(),
                    ])) if event.source == "price" or state.query() else "")
                ),
                "event_type_title": event_type_title,
                "event_explanation": event_explanation,
                "importance_title": importance_title,
                "to_local_datetime": to_event_local_datetime,
                "view_token": lambda event: view_state_token(
                    request,
                    site_id,
                    event.source,
                    event.event_id,
                    not event.is_viewed,
                ),
                "view_state_changed": request.query_params.get("view_state_changed") == "1",
                "json_export_url": f"/sites/{site_id}/changes/export.json"
                + (f"?{state.query(page=1)}" if state.query(page=1) else ""),
                "csv_export_url": f"/sites/{site_id}/changes/export.csv"
                + (f"?{state.query(page=1)}" if state.query(page=1) else ""),
            },
        )

    @application.get("/sites/{site_id}/changes/export.{format_name}")
    async def site_change_events_export(
        request: Request,
        site_id: int,
        format_name: str,
    ):
        if format_name not in {"json", "csv"}:
            return render_change_event_error(
                request,
                site_id,
                title="Формат экспорта не найден",
                message="Доступен экспорт только в JSON или CSV.",
                status_code=404,
            )
        if get_site(request.app.state.engine, site_id) is None:
            return render_site_not_found(request)
        state, errors = parse_change_event_list_state(
            event_type=request.query_params.get("event_type", ""),
            date_from=request.query_params.get("date_from", ""),
            date_to=request.query_params.get("date_to", ""),
            page=request.query_params.get("page", "1"),
            view_status=request.query_params.get("view_status", ""),
            local_timezone=active_settings.local_timezone,
        )
        if state is None:
            return render_change_event_error(
                request,
                site_id,
                title="Фильтры экспорта указаны неверно",
                message=" ".join(errors.values()),
                status_code=422,
            )
        return prepare_export_response(
            request,
            format_name,
            state,
            site_id=site_id,
        )

    async def render_global_change_events_screen(
        request: Request,
        site_type: str,
    ) -> HTMLResponse:
        section = SITE_SECTIONS[site_type]
        history_path = section["history_path"]
        sites = list_sites(request.app.state.engine, site_type)
        form = ChangeEventListForm(
            site_id=request.query_params.get("site_id", ""),
            event_type=request.query_params.get("event_type", ""),
            date_from=request.query_params.get("date_from", ""),
            date_to=request.query_params.get("date_to", ""),
            page=request.query_params.get("page", "1"),
            view_status=request.query_params.get("view_status", ""),
        )
        state, errors = parse_change_event_list_state(
            site_id=form.site_id,
            event_type=form.event_type,
            date_from=form.date_from,
            date_to=form.date_to,
            page=form.page,
            view_status=form.view_status,
            local_timezone=active_settings.local_timezone,
        )
        status_code = 422 if errors else 200
        if state is not None and state.site_id is not None:
            if all(site.id != state.site_id for site in sites):
                state = None
                errors = {"site_id": "Выбранный сайт не существует."}
                status_code = 404
        common_context = {
            "sites": sites,
            "section": section,
            "history_path": history_path,
            "scope": site_type,
            "event_page": None,
            "form": form,
            "errors": errors,
            "event_types": HISTORY_EVENT_TYPES,
            "event_type_title": event_type_title,
            "event_explanation": event_explanation,
            "importance_title": importance_title,
            "to_local_datetime": to_event_local_datetime,
        }
        if state is None or not sites:
            return templates.TemplateResponse(
                request=request,
                name="global_change_events.html",
                context=common_context,
                status_code=status_code,
            )
        try:
            offset = (state.page - 1) * EVENTS_PER_PAGE
            query_offset = offset if offset <= (1 << 63) - 1 else 0
            event_page = load_change_events(
                request.app.state.engine,
                site_id=state.site_id,
                site_type=site_type,
                event_types=(state.event_type,) if state.event_type else None,
                from_time=state.from_time,
                before_time=state.before_time,
                viewed=state.viewed,
                limit=EVENTS_PER_PAGE,
                offset=query_offset,
            )
            if state.page > 1 and offset >= event_page.total_count:
                return render_change_event_error(
                    request,
                    None,
                    title="Страница событий не найдена",
                    message=(
                        "Запрошенной страницы нет. "
                        "Перейдите к первой странице результатов."
                    ),
                    status_code=404,
                    return_url=global_change_event_list_url(
                        state, page=1, base_path=history_path
                    ),
                    action_label="К первой странице",
                )
            has_any_history = bool(event_page.total_count)
            if not has_any_history and (state.has_filters or state.site_id is not None):
                has_any_history = has_change_events(
                    request.app.state.engine,
                    site_type=site_type,
                )
        except ValueError as error:
            request.app.state.logger.error(
                "Повреждён общий список событий: %s",
                error,
            )
            return render_change_event_error(
                request,
                None,
                title="События нельзя показать",
                message=(
                    "Часть сохранённых данных повреждена. "
                    "Сохранённая история не изменена."
                ),
                status_code=500,
            )
        query = state.query()
        detail_suffix = f"?scope={site_type}" + (f"&{query}" if query else "")
        return templates.TemplateResponse(
            request=request,
            name="global_change_events.html",
            context={
                **common_context,
                "event_page": event_page,
                "errors": {},
                "state": state,
                "has_any_history": has_any_history,
                "current_page": state.page,
                "total_pages": max(
                    1,
                    (event_page.total_count + EVENTS_PER_PAGE - 1)
                    // EVENTS_PER_PAGE,
                ),
                "previous_url": (
                    global_change_event_list_url(
                        state,
                        page=state.page - 1,
                        base_path=history_path,
                    )
                    if state.page > 1
                    else None
                ),
                "next_url": (
                    global_change_event_list_url(
                        state,
                        page=state.page + 1,
                        base_path=history_path,
                    )
                    if state.page * EVENTS_PER_PAGE < event_page.total_count
                    else None
                ),
                "detail_url": lambda event: (
                    f"/sites/{event.site_id}/changes/{event.event_id}{detail_suffix}"
                    + ("&source=price" if event.source == "price" else "")
                ),
                "view_token": lambda event: view_state_token(
                    request,
                    event.site_id,
                    event.source,
                    event.event_id,
                    not event.is_viewed,
                ),
                "view_state_changed": request.query_params.get("view_state_changed") == "1",
                "json_export_url": f"{history_path}/export.json"
                + (f"?{state.query(page=1)}" if state.query(page=1) else ""),
                "csv_export_url": f"{history_path}/export.csv"
                + (f"?{state.query(page=1)}" if state.query(page=1) else ""),
            },
        )

    async def global_change_events_export_for_type(
        request: Request,
        format_name: str,
        site_type: str,
    ):
        if format_name not in {"json", "csv"}:
            return render_change_event_error(
                request,
                None,
                title="Формат экспорта не найден",
                message="Доступен экспорт только в JSON или CSV.",
                status_code=404,
            )
        state, errors = parse_change_event_list_state(
            site_id=request.query_params.get("site_id", ""),
            event_type=request.query_params.get("event_type", ""),
            date_from=request.query_params.get("date_from", ""),
            date_to=request.query_params.get("date_to", ""),
            page=request.query_params.get("page", "1"),
            view_status=request.query_params.get("view_status", ""),
            local_timezone=active_settings.local_timezone,
        )
        if state is None:
            return render_change_event_error(
                request,
                None,
                title="Фильтры экспорта указаны неверно",
                message=" ".join(errors.values()),
                status_code=422,
            )
        if state.site_id is not None and get_site_of_type(
            request.app.state.engine,
            state.site_id,
            site_type,
        ) is None:
            return render_change_event_error(
                request,
                None,
                title="Сайт не найден",
                message="Выбранный сайт не существует.",
                status_code=404,
            )
        return prepare_export_response(
            request,
            format_name,
            state,
            site_type=site_type,
        )

    @application.get("/changes")
    async def legacy_global_change_events(request: Request) -> RedirectResponse:
        query = str(request.query_params)
        return RedirectResponse(
            url="/competitors/changes" + (f"?{query}" if query else ""),
            status_code=307,
        )

    @application.get("/changes/export.{format_name}")
    async def legacy_global_change_events_export(
        request: Request,
        format_name: str,
    ) -> RedirectResponse:
        query = str(request.query_params)
        return RedirectResponse(
            url=f"/competitors/changes/export.{format_name}"
            + (f"?{query}" if query else ""),
            status_code=307,
        )

    @application.get("/competitors/changes", response_class=HTMLResponse)
    async def competitor_change_events(request: Request) -> HTMLResponse:
        return await render_global_change_events_screen(
            request,
            SITE_TYPE_COMPETITOR,
        )

    @application.get("/own-sites/changes", response_class=HTMLResponse)
    async def owned_change_events(request: Request) -> HTMLResponse:
        return await render_global_change_events_screen(request, SITE_TYPE_OWNED)

    @application.get("/competitors/changes/export.{format_name}")
    async def competitor_change_events_export(request: Request, format_name: str):
        return await global_change_events_export_for_type(
            request,
            format_name,
            SITE_TYPE_COMPETITOR,
        )

    @application.get("/own-sites/changes/export.{format_name}")
    async def owned_change_events_export(request: Request, format_name: str):
        return await global_change_events_export_for_type(
            request,
            format_name,
            SITE_TYPE_OWNED,
        )

    @application.get(
        "/sites/{site_id}/changes/{event_id}",
        response_class=HTMLResponse,
    )
    async def change_event_screen(
        request: Request,
        site_id: int,
        event_id: int,
    ) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        scope = request.query_params.get("scope", "")
        if scope not in {"", SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED}:
            return render_change_event_error(
                request,
                site_id,
                title="Параметры возврата указаны неверно",
                message="Источник списка событий указан неверно.",
                status_code=422,
            )
        if scope and site.site_type != scope:
            return render_site_not_found(request)
        state, errors = parse_change_event_list_state(
            site_id=(request.query_params.get("site_id", "") if scope else ""),
            event_type=request.query_params.get("event_type", ""),
            date_from=request.query_params.get("date_from", ""),
            date_to=request.query_params.get("date_to", ""),
            page=request.query_params.get("page", "1"),
            view_status=request.query_params.get("view_status", ""),
            local_timezone=active_settings.local_timezone,
        )
        if state is None:
            return render_change_event_error(
                request,
                site_id,
                title="Параметры возврата указаны неверно",
                message=" ".join(errors.values()),
                status_code=422,
            )
        if scope and state.site_id not in {None, site_id}:
            return render_change_event_error(
                request,
                site_id,
                title="Параметры возврата указаны неверно",
                message="Фильтр сайта не соответствует открытому событию.",
                status_code=422,
            )
        return_url = (
            global_change_event_list_url(
                state,
                base_path=SITE_SECTIONS[scope]["history_path"],
            )
            if scope
            else change_event_list_url(site_id, state)
        )
        source = request.query_params.get("source", "snapshot")
        if source not in {"snapshot", "price"}:
            return render_change_event_error(
                request,
                site_id,
                title="Параметры возврата указаны неверно",
                message="Источник события указан неверно.",
                status_code=422,
                return_url=return_url,
            )
        try:
            detail = load_change_event(
                request.app.state.engine,
                site_id=site_id,
                event_id=event_id,
                source=source,
            )
        except ChangeEventDataError as error:
            request.app.state.logger.error(
                "Повреждены данные события %s сайта %s: %s",
                event_id,
                site_id,
                error,
            )
            return render_change_event_error(
                request,
                site_id,
                title="Событие нельзя показать",
                message=(
                    "Связанные данные события повреждены. "
                    "Сохранённая история не изменена."
                ),
                status_code=500,
                return_url=return_url,
            )
        if detail is None:
            return render_change_event_error(
                request,
                site_id,
                title="Событие не найдено",
                message="В этом сайте такого события нет.",
                status_code=404,
                return_url=return_url,
            )
        current_side, previous_side = present_sides(detail)
        return templates.TemplateResponse(
            request=request,
            name="change_event_detail.html",
            context={
                "site": site,
                "detail": detail,
                "current_side": current_side,
                "previous_side": previous_side,
                "event_type_title": event_type_title,
                "importance_title": importance_title,
                "event_explanation": event_explanation,
                "to_local_datetime": to_event_local_datetime,
                "return_url": return_url,
                "state": state,
                "scope": scope,
                "source": source,
                "view_token": view_state_token(
                    request,
                    site_id,
                    source,
                    event_id,
                    detail.viewed_at is None,
                ),
                "view_state_changed": request.query_params.get("view_state_changed") == "1",
            },
        )

    @application.post(
        "/sites/{site_id}/changes/{event_id}/view-state",
        response_class=HTMLResponse,
    )
    async def change_event_view_state(
        request: Request,
        site_id: int,
        event_id: int,
    ) -> HTMLResponse:
        if event_id < 1:
            return render_change_event_error(
                request,
                site_id,
                title="Действие не выполнено",
                message="Идентификатор события указан неверно.",
                status_code=422,
            )
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        body = await request.body()
        if len(body) > 16 * 1024:
            return render_change_event_error(
                request,
                site_id,
                title="Действие не выполнено",
                message="Данные формы слишком велики.",
                status_code=413,
            )
        raw_form = parse_qs(
            body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        value = lambda name, default="": raw_form.get(name, [default])[0]
        source = value("source")
        action = value("action")
        scope = value("scope")
        return_area = value("return_area")
        if source not in {"snapshot", "price"} or action not in {"view", "unview"}:
            return render_change_event_error(
                request,
                site_id,
                title="Действие не выполнено",
                message="Источник или действие указаны неверно.",
                status_code=422,
            )
        if scope not in {"", SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED} or return_area not in {"list", "detail"}:
            return render_change_event_error(
                request,
                site_id,
                title="Действие не выполнено",
                message="Область возврата указана неверно.",
                status_code=422,
            )
        if scope and site.site_type != scope:
            return render_site_not_found(request)
        state, errors = parse_change_event_list_state(
            site_id=value("site_id") if scope else "",
            event_type=value("event_type"),
            date_from=value("date_from"),
            date_to=value("date_to"),
            page=value("page", "1"),
            view_status=value("view_status"),
            local_timezone=active_settings.local_timezone,
        )
        if state is None or (scope and state.site_id not in {None, site_id}):
            return render_change_event_error(
                request,
                site_id,
                title="Действие не выполнено",
                message="Параметры возврата указаны неверно.",
                status_code=422,
            )
        list_url = (
            global_change_event_list_url(
                state,
                base_path=SITE_SECTIONS[scope]["history_path"],
            )
            if scope
            else change_event_list_url(site_id, state)
        )
        detail_parts = []
        if scope:
            detail_parts.append(f"scope={scope}")
        if source == "price":
            detail_parts.append("source=price")
        state_query = state.query()
        if state_query:
            detail_parts.append(state_query)
        detail_url = f"/sites/{site_id}/changes/{event_id}"
        if detail_parts:
            detail_url += "?" + "&".join(detail_parts)
        return_url = detail_url if return_area == "detail" else list_url
        viewed = action == "view"
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            change_event_view_action(source, event_id, viewed),
            value("action_token"),
        ):
            return render_change_event_error(
                request,
                site_id,
                title="Действие запрещено",
                message="Подтверждение действия недействительно. Обновите страницу и повторите.",
                status_code=403,
                return_url=return_url,
            )
        result = set_change_event_viewed(
            request.app.state.engine,
            site_id=site_id,
            source=source,
            event_id=event_id,
            viewed=viewed,
        )
        if not result.found:
            return render_change_event_error(
                request,
                site_id,
                title="Событие не найдено",
                message="В этом сайте такого события нет.",
                status_code=404,
                return_url=list_url,
            )
        separator = "&" if "?" in return_url else "?"
        return RedirectResponse(
            url=f"{return_url}{separator}view_state_changed=1",
            status_code=303,
        )

    @application.post("/sites/{site_id}/edit", response_class=HTMLResponse)
    async def save_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)

        raw_form = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        form = {
            "name": raw_form.get("name", [""])[0],
            "url": raw_form.get("url", [""])[0],
        }
        errors = validate_site(form["name"], form["url"])
        if errors:
            return render_edit_site(
                request,
                site,
                form=form,
                errors=errors,
                status_code=422,
            )

        updated_site = update_site(
            request.app.state.engine,
            site_id,
            form["name"],
            form["url"],
        )
        if updated_site is None:
            return render_site_not_found(request)
        return RedirectResponse(
            url=f"{site_list_url(updated_site)}?updated=1",
            status_code=303,
        )

    @application.get("/sites/{site_id}/delete", response_class=HTMLResponse)
    async def confirm_delete_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        active_run = get_running_crawl_run(request.app.state.engine, site_id)
        if active_run is not None:
            return render_delete_running(request, site, active_run.id)
        (
            crawl_run_count,
            crawl_page_count,
            crawl_snapshot_count,
            crawl_price_count,
        ) = count_crawl_data(request.app.state.engine, site_id)
        schedule_count, scheduled_entry_count = count_schedule_data(
            request.app.state.engine, site_id
        )
        gsc_import_count, gsc_metric_count = count_import_data(
            request.app.state.engine, site_id
        )
        return templates.TemplateResponse(
            request=request,
            name="delete_site.html",
            context={
                "site": site,
                "history_count_text": format_check_count(
                    count_checks(request.app.state.engine, site_id)
                ),
                "crawl_run_count": crawl_run_count,
                "crawl_page_count": crawl_page_count,
                "crawl_snapshot_count": crawl_snapshot_count,
                "crawl_price_count": crawl_price_count,
                "schedule_count": schedule_count,
                "scheduled_entry_count": scheduled_entry_count,
                "gsc_import_count": gsc_import_count,
                "gsc_metric_count": gsc_metric_count,
                "confirmation_token": create_delete_confirmation_token(
                    request.app.state.action_token_secret,
                    site_id,
                ),
            },
        )

    @application.post("/sites/{site_id}/delete", response_class=HTMLResponse)
    async def remove_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)

        raw_form = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        confirmation_token = raw_form.get("confirmation_token", [""])[0]
        if not validate_delete_confirmation_token(
            request.app.state.action_token_secret,
            site_id,
            confirmation_token,
        ):
            return render_delete_forbidden(request, site)

        try:
            if not delete_site(request.app.state.engine, site_id):
                return render_site_not_found(request)
        except ActiveSiteCrawlError as error:
            return render_delete_running(
                request,
                site,
                error.run_id,
                status_code=409,
            )
        return RedirectResponse(
            url=f"{site_list_url(site)}?deleted=1",
            status_code=303,
        )

    @application.get("/sites/{site_id}/check", response_class=HTMLResponse)
    async def check_site_screen(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        return render_check_site(request, site)

    @application.post("/sites/{site_id}/check", response_class=HTMLResponse)
    async def run_site_check(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)

        raw_form = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        action_token = raw_form.get("action_token", [""])[0]
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            CHECK_AVAILABILITY_ACTION,
            action_token,
        ):
            return render_check_forbidden(request, site)

        check = start_check(request.app.state.engine, site_id)
        result = await request.app.state.availability_checker.check(site.url)
        complete_check(request.app.state.engine, check.id, result)
        return render_check_site(request, site, result=result)

    @application.get("/sites/{site_id}/crawl", response_class=HTMLResponse)
    async def crawl_site_screen(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        return render_crawl_screen(request, site)

    @application.get("/sites/{site_id}/schedule", response_class=HTMLResponse)
    async def schedule_screen(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        return render_schedule_screen(request, site)

    @application.post("/sites/{site_id}/schedule", response_class=HTMLResponse)
    async def update_schedule(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        raw = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        token = raw.get("action_token", [""])[0]
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            SAVE_SCHEDULE_ACTION,
            token,
        ):
            return render_schedule_screen(
                request,
                site,
                status_code=403,
                action_error="Токен изменения расписания недействителен.",
            )
        defaults = default_schedule_form(
            local_timezone=active_settings.local_timezone,
            now=clock(),
        )
        form = {
            key: raw.get(key, ["" if key == "enabled" else default])[0]
            for key, default in defaults.items()
        }
        values, errors = parse_schedule_form(form)
        if errors:
            return render_schedule_screen(
                request,
                site,
                form=form,
                errors=errors,
                status_code=422,
            )
        assert values is not None
        save_schedule(
            request.app.state.engine,
            site_id,
            values,
            now=clock(),
            local_timezone=active_settings.local_timezone,
        )
        await request.app.state.crawl_dispatcher.wake(now=clock())
        return RedirectResponse(
            url=f"/sites/{site_id}/schedule?saved=1",
            status_code=303,
        )

    @application.post(
        "/sites/{site_id}/schedule/{entry_id}/retry",
        response_class=HTMLResponse,
    )
    async def retry_scheduled_crawl(
        request: Request, site_id: int, entry_id: int
    ) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        raw = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            retry_scheduled_crawl_action(entry_id),
            raw.get("action_token", [""])[0],
        ):
            return render_schedule_screen(
                request,
                site,
                status_code=403,
                action_error="Токен ручного повтора недействителен.",
            )
        try:
            create_retry(
                request.app.state.engine,
                site_id,
                entry_id,
                now=clock(),
            )
        except LookupError:
            return render_schedule_screen(
                request,
                site,
                status_code=404,
                action_error="Запись журнала для этого сайта не найдена.",
            )
        except ValueError as error:
            return render_schedule_screen(
                request,
                site,
                status_code=409,
                action_error=str(error),
            )
        await request.app.state.crawl_dispatcher.wake(now=clock())
        return RedirectResponse(
            url=f"/sites/{site_id}/schedule?retried=1",
            status_code=303,
        )

    @application.post(
        "/sites/{site_id}/schedule/test-email",
        response_class=HTMLResponse,
    )
    async def test_schedule_email(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        raw = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            TEST_SMTP_ACTION,
            raw.get("action_token", [""])[0],
        ):
            return render_schedule_screen(
                request,
                site,
                status_code=403,
                action_error="Токен тестового письма недействителен.",
            )
        if not active_settings.smtp.enabled:
            return render_schedule_screen(
                request,
                site,
                status_code=409,
                action_error="SMTP не настроен и тестовое письмо недоступно.",
            )
        sent = await request.app.state.smtp_notifier.send_test()
        return RedirectResponse(
            url=f"/sites/{site_id}/schedule?email_test={'sent' if sent else 'failed'}",
            status_code=303,
        )

    @application.post("/sites/{site_id}/crawl", response_class=HTMLResponse)
    async def start_site_crawl(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)

        raw_form = parse_qs(
            (await request.body()).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        action_token = raw_form.get("action_token", [""])[0]
        if not validate_action_token(
            request.app.state.action_token_secret,
            site_id,
            START_CRAWL_ACTION,
            action_token,
        ):
            return render_crawl_forbidden(request, site)

        default_form = default_crawl_form()
        form = {
            field: raw_form.get(field, [default_value])[0]
            for field, default_value in default_form.items()
        }
        settings, errors = parse_crawl_settings(form)
        if errors:
            return render_crawl_screen(
                request,
                site,
                form=form,
                errors=errors,
                status_code=422,
            )
        assert settings is not None
        try:
            run = start_crawl_run(request.app.state.engine, site_id, settings)
        except ActiveCrawlRunError as error:
            return RedirectResponse(
                url=f"/crawl-runs/{error.run_id}?duplicate=1",
                status_code=303,
            )

        task = asyncio.create_task(
            perform_crawl(run.id, site, settings),
            name=f"crawl-run-{run.id}",
        )
        request.app.state.crawl_tasks.add(task)
        task.add_done_callback(request.app.state.crawl_tasks.discard)
        return RedirectResponse(url=f"/crawl-runs/{run.id}", status_code=303)

    @application.get("/sites/{site_id}/structure", response_class=HTMLResponse)
    async def site_structure_screen(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        raw = {
            "url": request.query_params.get("url", ""),
            "depth": request.query_params.get("depth", ""),
            "outcome": request.query_params.get("outcome", ""),
            "broken": request.query_params.get("broken", ""),
            "unchecked": request.query_params.get("unchecked", ""),
            "signal": request.query_params.get("signal", ""),
            "page": request.query_params.get("page", "1"),
        }
        state, errors = parse_structure_filters(**raw)
        try:
            selected = load_site_structure(request.app.state.engine, site_id)
        except StructureDataError as error:
            request.app.state.logger.error("Карта структуры повреждена: %s", error)
            return render_structure_error(
                request,
                site,
                title="Карту нельзя показать",
                message="Связанные данные выбранного обхода повреждены. Сохранённые данные не изменены.",
                status_code=500,
            )
        if state is None:
            state = StructureFilterState(
                url_value="", depth_value="", outcome_value="", broken_value="",
                unchecked_value="", signal_value="", page_value="1", depth=None,
                outcome=None, broken=None, unchecked=None, signal=None, page=1,
            )
        filtered = (
            filter_structure_pages(
                selected.structure.pages, selected.structure.analysis.pages, state
            )
            if selected else ()
        )
        total_pages = max(1, (len(filtered) + PAGES_PER_PAGE - 1) // PAGES_PER_PAGE)
        if not errors and filtered and state.page > total_pages:
            errors["page"] = "Номер страницы выходит за пределы результатов."
        page_number = min(state.page, total_pages)
        offset = (page_number - 1) * PAGES_PER_PAGE
        page_items = filtered[offset : offset + PAGES_PER_PAGE]
        graph_view = None
        graph_limited = len(filtered) > 100
        filtered_edges = selected.structure.edges_for(filtered) if selected else ()
        filtered_by_id = {item.record_id: item for item in filtered}
        graph_edge_rows = tuple(
            (filtered_by_id[edge.source_record_id], filtered_by_id[edge.target_record_id])
            for edge in filtered_edges
        )
        if selected and filtered and not graph_limited:
            graph_view = build_graph_view(filtered, filtered_edges)
        return templates.TemplateResponse(
            request=request,
            name="site_structure.html",
            context={
                "site": site,
                "selected": selected,
                "state": state,
                "form": raw,
                "errors": errors,
                "filtered": filtered,
                "page_items": page_items,
                "total_pages": total_pages,
                "page_number": page_number,
                "tree": selected.structure.tree_for(filtered) if selected else None,
                "graph_view": graph_view,
                "graph_edges": filtered_edges,
                "graph_edge_rows": graph_edge_rows,
                "graph_limited": graph_limited,
                "outcome_titles": OUTCOME_TITLES,
                "signal_titles": SIGNAL_TITLES,
                "signal_counts": {
                    signal: selected.structure.analysis.signal_count(signal)
                    for signal in StructuralSignal
                } if selected else {},
                "analysis_by_id": {
                    item.record_id: item for item in selected.structure.analysis.pages
                } if selected else {},
                "structure_url": structure_url,
                "to_local_datetime": to_event_local_datetime,
                "state_query": state.query(),
            },
            status_code=422 if errors else 200,
        )

    @application.get(
        "/sites/{site_id}/structure/pages/{page_id}", response_class=HTMLResponse
    )
    async def site_structure_page_detail(
        request: Request, site_id: int, page_id: int
    ) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        raw = {
            "url": request.query_params.get("url", ""),
            "depth": request.query_params.get("depth", ""),
            "outcome": request.query_params.get("outcome", ""),
            "broken": request.query_params.get("broken", ""),
            "unchecked": request.query_params.get("unchecked", ""),
            "signal": request.query_params.get("signal", ""),
            "page": request.query_params.get("page", "1"),
        }
        state, errors = parse_structure_filters(**raw)
        if state is None or errors:
            return render_structure_error(
                request,
                site,
                title="Параметры возврата неверны",
                message="Откройте страницу заново из карты структуры.",
                status_code=422,
            )
        try:
            selected = load_site_structure(request.app.state.engine, site_id)
        except StructureDataError as error:
            request.app.state.logger.error("Подробности структуры повреждены: %s", error)
            return render_structure_error(
                request,
                site,
                title="Подробности нельзя показать",
                message="Связанные данные выбранного обхода повреждены. Сохранённые данные не изменены.",
                status_code=500,
                return_url=structure_url(site_id, state),
            )
        if selected is None:
            return render_structure_error(
                request,
                site,
                title="Подходящий обход не найден",
                message="Для карты нужен завершённый или частично завершённый обход.",
                status_code=404,
            )
        page = selected.structure.page_by_id(page_id)
        if page is None:
            return render_structure_error(
                request,
                site,
                title="Страница не найдена",
                message="В выбранном обходе такой страницы нет.",
                status_code=404,
                return_url=structure_url(site_id, state),
            )
        by_id = {item.record_id: item for item in selected.structure.pages}
        incoming = tuple(by_id[item] for item in page.incoming_record_ids)
        page_analysis = selected.structure.analysis.page_by_id(page_id)
        assert page_analysis is not None
        return templates.TemplateResponse(
            request=request,
            name="site_structure_detail.html",
            context={
                "site": site,
                "selected": selected,
                "page": page,
                "incoming": incoming,
                "page_analysis": page_analysis,
                "signal_titles": SIGNAL_TITLES,
                "cycle_component": tuple(
                    by_id[item] for item in page_analysis.cycle_component_record_ids
                ),
                "return_url": structure_url(site_id, state),
                "state_query": state.query(),
                "external_url": safe_external_url(page.url),
                "to_local_datetime": to_event_local_datetime,
            },
        )

    @application.get("/crawl-runs/{run_id}", response_class=HTMLResponse)
    async def crawl_run_screen(request: Request, run_id: int) -> HTMLResponse:
        run = get_crawl_run(request.app.state.engine, run_id)
        if run is None:
            return render_crawl_not_found(request)
        site = get_site(request.app.state.engine, run.site_id)
        if site is None:
            return render_crawl_not_found(request)
        crawl_errors = list_crawl_errors(request.app.state.engine, run_id)
        return templates.TemplateResponse(
            request=request,
            name="crawl_run.html",
            context={
                "site": site,
                "run": run,
                "crawl_errors": crawl_errors,
                "is_running": run.status == RUNNING_STATUS,
                "duplicate": request.query_params.get("duplicate") == "1",
                "to_local_datetime": to_local_datetime,
                "status_title": crawl_status_title,
                "error_outcome_title": crawl_error_outcome_title,
                "has_structure": (
                    run.status != RUNNING_STATUS
                    and has_site_structure(request.app.state.engine, site.id)
                ),
            },
        )

    return application


app = create_app()
