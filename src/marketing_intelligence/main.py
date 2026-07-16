"""Точка сборки минимального FastAPI-приложения."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
from .confirmation import (
    CHECK_AVAILABILITY_ACTION,
    START_CRAWL_ACTION,
    create_action_token,
    create_delete_confirmation_token,
    validate_delete_confirmation_token,
    validate_action_token,
)
from .config import Settings
from .database import build_engine, initialize_database
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
from .logging_config import configure_logging
from .models import Site
from .sites import (
    ActiveSiteCrawlError,
    add_site,
    delete_site,
    get_site,
    list_sites,
    update_site,
    validate_site,
)


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(
    settings: Settings | None = None,
    *,
    availability_checker: AvailabilityChecker | None = None,
    crawler: Crawler | None = None,
) -> FastAPI:
    """Создать приложение с переданными или локальными настройками."""

    active_settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_settings.data_dir.mkdir(parents=True, exist_ok=True)
        active_settings.logs_dir.mkdir(parents=True, exist_ok=True)

        logger = configure_logging(active_settings.logs_dir)
        engine = build_engine(active_settings.database_url)
        initialize_database(engine)
        recover_interrupted_runs(engine)

        application.state.settings = active_settings
        application.state.engine = engine
        application.state.logger = logger
        application.state.action_token_secret = secrets.token_bytes(32)
        application.state.availability_checker = availability_checker or AvailabilityChecker()
        application.state.crawler = crawler or Crawler()
        application.state.crawl_tasks = set()
        logger.info("Marketing Intelligence запущен")

        try:
            yield
        finally:
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
        *,
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "sites": list_sites(request.app.state.engine),
                "form": form or {"name": "", "url": ""},
                "errors": errors or {},
                "created": request.query_params.get("created") == "1",
                "updated": request.query_params.get("updated") == "1",
                "deleted": request.query_params.get("deleted") == "1",
            },
            status_code=status_code,
        )

    @application.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return render_sites(request)

    @application.post("/sites", response_class=HTMLResponse)
    async def create_site(request: Request) -> HTMLResponse:
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
            return render_sites(request, form=form, errors=errors, status_code=422)

        add_site(request.app.state.engine, form["name"], form["url"])
        return RedirectResponse(url="/?created=1", status_code=303)

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
                "Фоновый обход завершился ошибкой (run_id=%s)", run_id
            )

    @application.get("/sites/{site_id}/edit", response_class=HTMLResponse)
    async def edit_site(request: Request, site_id: int) -> HTMLResponse:
        site = get_site(request.app.state.engine, site_id)
        if site is None:
            return render_site_not_found(request)
        return render_edit_site(request, site)

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
        return RedirectResponse(url="/?updated=1", status_code=303)

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
        return RedirectResponse(url="/?deleted=1", status_code=303)

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
            },
        )

    return application


app = create_app()
