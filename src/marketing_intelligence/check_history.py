"""Хранение истории ручных проверок доступности."""

from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy import func
from sqlmodel import Session, select

from .availability import AvailabilityResult
from .models import AvailabilityCheck


RUNNING_STATUS = "running"


def start_check(engine: Engine, site_id: int) -> AvailabilityCheck:
    """Зафиксировать начало подтверждённой проверки."""

    check = AvailabilityCheck(
        site_id=site_id,
        status=RUNNING_STATUS,
        message="Проверка выполняется.",
    )
    with Session(engine) as session:
        session.add(check)
        session.commit()
        session.refresh(check)
        return check


def complete_check(
    engine: Engine,
    check_id: int,
    result: AvailabilityResult,
) -> AvailabilityCheck:
    """Сохранить завершение и результат ранее начатой проверки."""

    with Session(engine) as session:
        check = session.get(AvailabilityCheck, check_id)
        if check is None:
            raise LookupError("Начатая проверка доступности не найдена.")

        check.completed_at = datetime.now(UTC)
        check.status = result.status.value
        check.message = result.message
        check.robots_status = result.robots_status
        check.page_status = result.page_status
        session.add(check)
        session.commit()
        session.refresh(check)
        return check


def list_checks(engine: Engine, site_id: int) -> list[AvailabilityCheck]:
    """Вернуть историю сайта от новой записи к старой."""

    with Session(engine) as session:
        statement = (
            select(AvailabilityCheck)
            .where(AvailabilityCheck.site_id == site_id)
            .order_by(AvailabilityCheck.started_at.desc(), AvailabilityCheck.id.desc())
        )
        return list(session.exec(statement).all())


def count_checks(engine: Engine, site_id: int) -> int:
    """Вернуть число записей истории выбранного сайта."""

    with Session(engine) as session:
        statement = (
            select(func.count())
            .select_from(AvailabilityCheck)
            .where(AvailabilityCheck.site_id == site_id)
        )
        return session.exec(statement).one()


def format_check_count(count: int) -> str:
    """Согласовать число записей истории для предупреждения."""

    remainder_100 = count % 100
    remainder_10 = count % 10
    if remainder_10 == 1 and remainder_100 != 11:
        word = "запись"
    elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        word = "записи"
    else:
        word = "записей"
    return f"{count} {word}"
