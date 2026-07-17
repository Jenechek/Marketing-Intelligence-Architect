"""Оркестрация обработки успешно завершённого обхода."""

from sqlalchemy.engine import Engine

from .change_event_persistence import save_snapshot_comparison_events
from .snapshot_comparison_aggregation import build_completed_snapshot_comparison
from .snapshot_pair_storage import load_completed_snapshot_comparison_input


def process_completed_crawl_run(engine: Engine, run_id: int) -> int:
    """Сравнить completed-запуск с предыдущим и сохранить новые события."""

    comparison_input = load_completed_snapshot_comparison_input(engine, run_id)
    comparison = build_completed_snapshot_comparison(comparison_input)
    return save_snapshot_comparison_events(engine, comparison)
