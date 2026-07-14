"""Точка сборки минимального FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .availability import AvailabilityChecker, AvailabilityResult
from .confirmation import (
    CHECK_AVAILABILITY_ACTION,
    create_action_token,
    create_delete_confirmation_token,
    validate_delete_confirmation_token,
    validate_action_token,
)
from .config import Settings
from .database import build_engine, initialize_database
from .logging_config import configure_logging
from .models import Site
from .sites import add_site, delete_site, get_site, list_sites, update_site, validate_site


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(
    settings: Settings | None = None,
    *,
    availability_checker: AvailabilityChecker | None = None,
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

        application.state.settings = active_settings
        application.state.engine = engine
        application.state.action_token_secret = secrets.token_bytes(32)
        application.state.availability_checker = availability_checker or AvailabilityChecker()
        logger.info("Marketing Intelligence запущен")

        try:
            yield
        finally:
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
        return templates.TemplateResponse(
            request=request,
            name="delete_site.html",
            context={
                "site": site,
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

        if not delete_site(request.app.state.engine, site_id):
            return render_site_not_found(request)
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

        result = await request.app.state.availability_checker.check(site.url)
        return render_check_site(request, site, result=result)

    return application


app = create_app()
