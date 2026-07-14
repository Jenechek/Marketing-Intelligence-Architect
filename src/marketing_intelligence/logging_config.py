"""Базовая настройка локального журнала приложения."""

import logging
from pathlib import Path


LOGGER_NAME = "marketing_intelligence"


def configure_logging(logs_dir: Path) -> logging.Logger:
    """Писать сообщения приложения в файл и в консоль."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_marketing_intelligence_handler", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        logs_dir / "marketing-intelligence.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._marketing_intelligence_handler = True  # type: ignore[attr-defined]

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler._marketing_intelligence_handler = True  # type: ignore[attr-defined]

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
