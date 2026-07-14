"""Точка сборки минимального FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .database import build_engine, initialize_database
from .logging_config import configure_logging


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

    @application.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "status": "Система готова",
                "next_step": "Проверьте стартовую страницу, затем подтвердите готовность этапа 0.",
            },
        )

    return application


app = create_app()
