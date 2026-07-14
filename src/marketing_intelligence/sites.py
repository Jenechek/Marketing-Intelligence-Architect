"""Правила и операции управления сайтами."""

from urllib.parse import urlsplit

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .models import Site


def validate_site(name: str, url: str) -> dict[str, str]:
    """Проверить базовые данные сайта и вернуть понятные ошибки формы."""

    errors: dict[str, str] = {}
    clean_name = name.strip()
    clean_url = url.strip()

    if not clean_name:
        errors["name"] = "Укажите понятное название сайта."

    if not clean_url:
        errors["url"] = "Укажите URL сайта."
        return errors

    if any(character.isspace() for character in clean_url):
        errors["url"] = "URL не должен содержать пробелы."
        return errors

    try:
        parsed_url = urlsplit(clean_url)
        _ = parsed_url.port
    except ValueError:
        errors["url"] = "Проверьте адрес и номер порта."
        return errors

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        errors["url"] = "Введите полный URL, например https://example.com."
    elif parsed_url.username or parsed_url.password:
        errors["url"] = "Не указывайте логин или пароль в URL."

    return errors


def list_sites(engine: Engine) -> list[Site]:
    """Вернуть добавленные сайты, начиная с последнего."""

    with Session(engine) as session:
        statement = select(Site).order_by(Site.created_at.desc(), Site.id.desc())
        return list(session.exec(statement).all())


def get_site(engine: Engine, site_id: int) -> Site | None:
    """Вернуть сайт по идентификатору или ``None``, если сайт не найден."""

    with Session(engine) as session:
        return session.get(Site, site_id)


def add_site(engine: Engine, name: str, url: str) -> Site:
    """Сохранить проверенные данные сайта одной транзакцией."""

    site = Site(name=name.strip(), url=url.strip())
    with Session(engine) as session:
        session.add(site)
        session.commit()
        session.refresh(site)
        return site


def update_site(engine: Engine, site_id: int, name: str, url: str) -> Site | None:
    """Обновить проверенные данные сайта одной транзакцией."""

    with Session(engine) as session:
        site = session.get(Site, site_id)
        if site is None:
            return None

        site.name = name.strip()
        site.url = url.strip()
        session.add(site)
        session.commit()
        session.refresh(site)
        return site


def delete_site(engine: Engine, site_id: int) -> bool:
    """Окончательно удалить выбранный сайт одной транзакцией."""

    with Session(engine) as session:
        site = session.get(Site, site_id)
        if site is None:
            return False

        session.delete(site)
        session.commit()
        return True
