"""Точка сборки минимального FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .database import build_engine, initialize_database
from .logging_config import configure_logging
from .sites import add_site, list_sites, validate_site


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(settings: Settings | None = None) -> FastAPI:
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

    return application


app = create_app()
