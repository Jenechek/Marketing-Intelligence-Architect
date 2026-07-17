"""Настройки локального приложения."""

from dataclasses import dataclass
from datetime import tzinfo
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Пути, которые можно заменить для тестов или отдельной установки."""

    data_dir: Path
    logs_dir: Path
    database_url: str
    local_timezone: tzinfo | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        data_dir = Path(os.getenv("MI_DATA_DIR", "data"))
        logs_dir = Path(os.getenv("MI_LOGS_DIR", "logs"))
        database_url = os.getenv("MI_DATABASE_URL")

        if database_url is None:
            database_path = (data_dir / "marketing_intelligence.db").resolve()
            database_url = f"sqlite:///{database_path.as_posix()}"

        return cls(
            data_dir=data_dir,
            logs_dir=logs_dir,
            database_url=database_url,
        )
