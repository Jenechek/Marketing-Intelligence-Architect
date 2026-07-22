"""Безопасная общая основа read-only поисковых интеграций."""

from __future__ import annotations

import base64
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import os
from pathlib import Path
import secrets
from urllib.parse import quote, urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
import httpx
from sqlalchemy import delete, func, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .config import OAuthProviderConfig
from .models import (IntegrationConnection, IntegrationOAuthAttempt,
    IntegrationPageMetric, IntegrationSchedule, IntegrationSource,
    IntegrationSyncRun, Site, SITE_TYPE_OWNED)
from .link_discovery import normalize_http_url, url_origin
from .scheduler import advance_run

GOOGLE = "google_search_console"
YANDEX = "yandex_webmaster"
PROVIDERS = (GOOGLE, YANDEX)
GOOGLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
YANDEX_SCOPE = "webmaster:read"
MAX_RESPONSE = 2 * 1024 * 1024
MAX_ROWS = 10_000
GOOGLE_PAGE_SIZE = 1_000
YANDEX_PAGE_SIZE = 500


class IntegrationError(RuntimeError):
    def __init__(self, message: str, code: str = "integration_error") -> None:
        super().__init__(message); self.code = code


class TokenLocks:
    """Сериализовать обновление токена внутри локального процесса."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, connection_id: int) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(connection_id, asyncio.Lock())


class SecretBox:
    def __init__(self, data_dir: Path, configured_key: str | None = None) -> None:
        key_path = data_dir / "integration.key"
        try:
            key = (configured_key or "").encode("ascii") if configured_key else None
            if key is None:
                data_dir.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    key = key_path.read_bytes().strip()
                else:
                    key = Fernet.generate_key()
                    with os.fdopen(fd, "wb") as stream: stream.write(key)
                    if os.name != "nt": os.chmod(key_path, 0o600)
            self._fernet = Fernet(key)
        except (ValueError, UnicodeError, OSError) as exc:
            raise IntegrationError("Ключ интеграций недоступен или имеет неверный формат.", "invalid_key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode("ascii")

    def decrypt(self, value: str) -> str:
        try: return self._fernet.decrypt(value.encode("ascii")).decode()
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise IntegrationError("Требуется повторное подключение: защищённые данные недоступны.", "decrypt_failed") from exc


def provider_config(settings, provider: str) -> OAuthProviderConfig:
    if provider == GOOGLE: return settings.google_oauth
    if provider == YANDEX: return settings.yandex_oauth
    raise IntegrationError("Неизвестная интеграция.", "invalid_provider")


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def start_oauth(engine: Engine, box: SecretBox, site_id: int, provider: str,
                config: OAuthProviderConfig, *, another: bool = False,
                now: datetime | None = None) -> str:
    if not config.enabled: raise IntegrationError(config.error or "OAuth-клиент не настроен.", "not_configured")
    moment = now or datetime.now(UTC); state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64) if provider == YANDEX else None
    with Session(engine) as session:
        site = session.get(Site, site_id)
        if not site or site.site_type != SITE_TYPE_OWNED: raise IntegrationError("Собственный сайт не найден.", "not_found")
        session.add(IntegrationOAuthAttempt(site_id=site_id, provider=provider,
            action="another" if another else "connect", state_hash=hashlib.sha256(state.encode()).hexdigest(),
            pkce_verifier_encrypted=box.encrypt(verifier) if verifier else None,
            created_at=moment, expires_at=moment + timedelta(minutes=10)))
        session.commit()
    if provider == GOOGLE:
        params = {"client_id":config.client_id,"redirect_uri":config.redirect_uri,"response_type":"code",
            "scope":GOOGLE_SCOPE,"access_type":"offline","state":state,"include_granted_scopes":"true"}
        if another: params.update(prompt="consent select_account")
        else: params["prompt"] = "consent"
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    params = {"response_type":"code","client_id":config.client_id,"redirect_uri":config.redirect_uri,
        "scope":YANDEX_SCOPE,"state":state,"code_challenge":_challenge(verifier or ""),"code_challenge_method":"S256"}
    if another: params["force_confirm"] = "yes"
    return "https://oauth.yandex.com/authorize?" + urlencode(params)


def consume_attempt(engine: Engine, box: SecretBox, provider: str, state: str,
                    now: datetime | None = None) -> tuple[IntegrationOAuthAttempt, str | None]:
    moment=now or datetime.now(UTC); digest=hashlib.sha256(state.encode()).hexdigest()
    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        attempt=session.exec(select(IntegrationOAuthAttempt).where(IntegrationOAuthAttempt.state_hash==digest)).one_or_none()
        if not attempt or attempt.provider != provider or attempt.used_at or attempt.expires_at < moment:
            raise IntegrationError("OAuth-запрос недействителен или уже использован.", "invalid_state")
        site=session.get(Site, attempt.site_id)
        if not site or site.site_type != SITE_TYPE_OWNED: raise IntegrationError("Собственный сайт не найден.", "not_found")
        verifier=box.decrypt(attempt.pkce_verifier_encrypted) if attempt.pkce_verifier_encrypted else None
        attempt.used_at=moment; session.add(attempt); session.commit(); session.refresh(attempt)
        return attempt, verifier


async def safe_json(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    try: response=await client.request(method, url, follow_redirects=False, timeout=15, **kwargs)
    except httpx.HTTPError as exc: raise IntegrationError("Внешний сервис временно недоступен.", "network_error") from exc
    if len(response.content)>MAX_RESPONSE or "json" not in response.headers.get("content-type","").lower(): raise IntegrationError("Внешний сервис вернул недопустимый ответ.", "invalid_response")
    try: data=response.json()
    except ValueError as exc: raise IntegrationError("Внешний сервис вернул некорректные данные.", "invalid_json") from exc
    if not isinstance(data,dict): raise IntegrationError("Внешний сервис вернул некорректную структуру.", "invalid_json")
    provider_code=data.get("error")
    if response.status_code == 401: raise IntegrationError("Токен доступа отклонён провайдером.", "unauthorized")
    if provider_code == "invalid_grant": raise IntegrationError("Доступ отозван или истёк. Подключите аккаунт повторно.", "invalid_grant")
    if response.status_code == 429 or response.status_code >= 500: raise IntegrationError("Внешний сервис временно ограничил запрос.", "retryable")
    if response.status_code >= 400: raise IntegrationError("Внешний сервис отклонил запрос.", "provider_error")
    return data


async def finish_oauth(engine: Engine, box: SecretBox, settings, provider: str, state: str,
                       code: str, client: httpx.AsyncClient) -> int:
    attempt, verifier=consume_attempt(engine,box,provider,state); config=provider_config(settings,provider)
    url="https://oauth2.googleapis.com/token" if provider==GOOGLE else "https://oauth.yandex.com/token"
    form={"grant_type":"authorization_code","code":code,"client_id":config.client_id,
          "client_secret":config.client_secret,"redirect_uri":config.redirect_uri}
    if verifier: form["code_verifier"]=verifier
    token=await safe_json(client,"POST",url,data=form)
    access=token.get("access_token"); refresh=token.get("refresh_token")
    if not isinstance(access,str) or not access or not isinstance(refresh,str) or not refresh: raise IntegrationError("Провайдер не вернул данные для автономного доступа.", "missing_token")
    expires=token.get("expires_in",3600)
    if not isinstance(expires,(int,float)) or expires<=0: expires=3600
    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        site=session.get(Site,attempt.site_id)
        if not site or site.site_type != SITE_TYPE_OWNED:
            raise IntegrationError("Собственный сайт удалён во время подключения.", "connection_changed")
        connection=session.exec(select(IntegrationConnection).where(IntegrationConnection.site_id==attempt.site_id,IntegrationConnection.provider==provider)).one_or_none()
        if connection is None: connection=IntegrationConnection(site_id=attempt.site_id,provider=provider)
        else: connection.revision += 1
        connection.status="connected"; connection.access_token_encrypted=box.encrypt(access); connection.refresh_token_encrypted=box.encrypt(refresh)
        connection.token_expires_at=datetime.now(UTC)+timedelta(seconds=min(int(expires),86400)); connection.last_error=None; connection.updated_at=datetime.now(UTC)
        connection.provider_user_id=None
        session.add(connection); session.flush()
        session.exec(update(IntegrationSource).where(IntegrationSource.connection_id==connection.id,IntegrationSource.active==True).values(active=False))
        session.exec(update(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id,IntegrationSyncRun.status=="pending").values(status="cancelled",completed_at=datetime.now(UTC),message="Запуск отменён после подключения другого OAuth-аккаунта."))
        session.commit(); session.refresh(connection); return connection.id or 0


def _host(value: str) -> str | None:
    raw=value[10:] if value.lower().startswith("sc-domain:") else value
    try: host=(urlsplit(raw if "://" in raw else "https://"+raw).hostname or "").rstrip(".").lower().encode("idna").decode()
    except (ValueError,UnicodeError): return None
    return host[4:] if host.startswith("www.") else host


def matching_resources(site_url: str, provider: str, resources: list[dict]) -> list[dict]:
    target=_host(site_url); matches=[]; seen=set()
    for resource in resources:
        if provider == YANDEX:
            candidates=[]
            if resource.get("verified") is True:
                candidates.append(resource)
            mirror=resource.get("main_mirror")
            if isinstance(mirror,dict) and mirror.get("verified") is True:
                candidates.append(mirror)
            for candidate in candidates:
                rid=candidate.get("host_id"); label=candidate.get("unicode_host_url") or candidate.get("ascii_host_url") or rid
                if isinstance(rid,str) and isinstance(label,str) and rid not in seen and any(_host(str(candidate.get(key) or ""))==target for key in ("host_id","ascii_host_url","unicode_host_url")):
                    matches.append({"id":rid,"label":label});seen.add(rid)
            continue
        rid=resource.get("id") or resource.get("siteUrl") or resource.get("host_id")
        label=resource.get("label") or resource.get("unicode_host_url") or resource.get("ascii_host_url") or rid
        permission=str(resource.get("permissionLevel") or resource.get("verification") or resource.get("verified") or "").lower()
        if not isinstance(rid,str) or not isinstance(label,str): continue
        if provider==GOOGLE and permission in {"siteunverifieduser","none"}: continue
        candidates=[rid,label,str(resource.get("ascii_host_url") or "")]
        if target and rid not in seen and any(_host(v)==target for v in candidates if v): matches.append({"id":rid,"label":label});seen.add(rid)
    return matches


def automatic_resource(site_url:str,provider:str,resources:list[dict]) -> dict | None:
    if not resources:return None
    if provider==GOOGLE:
        own=normalize_http_url(site_url); own_origin=url_origin(own) if own else None
        exact_property=f"{own_origin}/" if own_origin else None
        exact=[];domain=[]
        for resource in resources:
            rid=resource["id"]
            normalized=normalize_http_url(rid)
            if normalized and normalized==exact_property:exact.append(resource)
            elif rid.lower().startswith("sc-domain:"):domain.append(resource)
        best=exact or domain
    else:best=resources
    return best[0] if len(best)==1 else None


def select_resource(engine: Engine, connection_id: int, resource: dict, *, expected_source_id: int | None = None, expected_revision: int | None = None) -> IntegrationSource:
    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        connection=session.get(IntegrationConnection,connection_id)
        site=session.get(Site,connection.site_id) if connection else None
        if not connection or connection.status != "connected" or not site or site.site_type != SITE_TYPE_OWNED:
            raise IntegrationError("Подключение недоступно.","connection_changed")
        if expected_revision is not None and connection.revision != expected_revision:
            raise IntegrationError("Подключение изменилось в другом окне. Обновите страницу.","connection_changed")
        current=session.exec(select(IntegrationSource).where(IntegrationSource.connection_id==connection_id,IntegrationSource.active==True)).one_or_none()
        current_id=current.id if current else None
        if current_id != expected_source_id:
            raise IntegrationError("Ресурс уже изменён в другом окне. Обновите страницу.","stale_resource")
        if current and current.resource_id == resource["id"]:
            session.commit()
            session.refresh(current)
            return current
        session.exec(update(IntegrationSource).where(IntegrationSource.connection_id==connection_id,IntegrationSource.active==True).values(active=False))
        session.exec(update(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection_id,IntegrationSyncRun.status=="pending").values(status="cancelled",completed_at=datetime.now(UTC),message="Запуск отменён после смены ресурса."))
        version=(session.exec(select(func.max(IntegrationSource.version)).where(IntegrationSource.connection_id==connection_id)).one() or 0)+1
        source=IntegrationSource(connection_id=connection_id,provider=connection.provider,version=version,resource_id=resource["id"],resource_label=resource["label"])
        connection.revision += 1; connection.updated_at=datetime.now(UTC); session.add(connection)
        session.add(source); session.flush()
        if session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection_id)).one_or_none() is None:
            session.add(IntegrationSchedule(connection_id=connection_id))
        session.add(IntegrationSyncRun(connection_id=connection_id,source_id=source.id,trigger="initial")); session.commit(); session.refresh(source); return source


def enqueue_sync(engine: Engine, connection_id: int, source_id: int, trigger: str, *, expected_revision: int | None = None) -> IntegrationSyncRun:
    """Идемпотентно поставить один запуск подключения в сохраняемую очередь."""

    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        connection = session.get(IntegrationConnection, connection_id)
        source = session.get(IntegrationSource, source_id)
        site = session.get(Site, connection.site_id) if connection else None
        if not connection or connection.status != "connected" or (expected_revision is not None and connection.revision != expected_revision) or not source or not source.active or source.connection_id != connection_id or not site or site.site_type != SITE_TYPE_OWNED:
            raise IntegrationError("Подключение или активный ресурс недоступны.", "connection_changed")
        active = session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection_id,IntegrationSyncRun.status.in_(("pending","running"))).order_by(IntegrationSyncRun.id)).first()
        if active:
            session.rollback()
            return active
        run = IntegrationSyncRun(connection_id=connection_id, source_id=source_id, trigger=trigger)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


async def available_resources(engine: Engine, box: SecretBox, connection_id: int,
                              client: httpx.AsyncClient, *, persist_user: bool = False,
                              settings=None, token_locks: TokenLocks | None = None) -> list[dict]:
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,connection_id)
        if not connection or connection.status != "connected" or not connection.access_token_encrypted: raise IntegrationError("Подключение не найдено.","not_found")
        site=session.get(Site,connection.site_id)
        if not site or site.site_type != SITE_TYPE_OWNED: raise IntegrationError("Собственный сайт не найден.","not_found")
        site_url=site.url
        if settings is None and connection.token_expires_at and connection.token_expires_at <= datetime.now(UTC):
            raise IntegrationError("Срок доступа истёк. Безопасно обновите доступ, чтобы продолжить.","token_refresh_required")
        token=box.decrypt(connection.access_token_encrypted)
        provider=connection.provider
        revision=connection.revision
    async def call(method,url,**kwargs):
        if settings is not None:
            return await authenticated_json(engine,box,settings,connection_id,client,token_locks or TokenLocks(),method,url,now=datetime.now(UTC),**kwargs)
        return await safe_json(client,method,url,headers={"Authorization":f"Bearer {token}"},**kwargs)
    if provider==GOOGLE:
        data=await call("GET","https://www.googleapis.com/webmasters/v3/sites")
        raw=data.get("siteEntry",[])
    else:
        user=await call("GET","https://api.webmaster.yandex.net/v4/user")
        uid=user.get("user_id")
        if isinstance(uid,bool) or not isinstance(uid,int) or uid<0: raise IntegrationError("Яндекс не вернул корректный идентификатор пользователя.","invalid_response")
        data=await call("GET",f"https://api.webmaster.yandex.net/v4/user/{uid}/hosts")
        raw=data.get("hosts",[])
        if persist_user:
            with Session(engine) as session:
                if engine.dialect.name == "sqlite":
                    session.exec(text("BEGIN IMMEDIATE"))
                connection=session.get(IntegrationConnection,connection_id)
                site=session.get(Site,connection.site_id) if connection else None
                if not connection or connection.status != "connected" or connection.revision != revision or not site or site.site_type != SITE_TYPE_OWNED:
                    raise IntegrationError("Подключение изменилось во время получения ресурсов.","connection_changed")
                connection.provider_user_id=str(uid); session.add(connection); session.commit()
    if not isinstance(raw,list) or len(raw)>MAX_ROWS: raise IntegrationError("Список ресурсов имеет недопустимый размер.","invalid_response")
    return matching_resources(site_url,provider,[x for x in raw if isinstance(x,dict)])


def snapshot(engine: Engine, site_id: int) -> dict[str, dict]:
    with Session(engine) as session:
        result={}
        for provider in PROVIDERS:
            connection=session.exec(select(IntegrationConnection).where(IntegrationConnection.site_id==site_id,IntegrationConnection.provider==provider)).one_or_none()
            source=None; last=None; last_success=None; schedule=None; recent_runs=[]
            attempt=session.exec(select(IntegrationOAuthAttempt).where(IntegrationOAuthAttempt.site_id==site_id,IntegrationOAuthAttempt.provider==provider,IntegrationOAuthAttempt.used_at==None,IntegrationOAuthAttempt.expires_at>datetime.now(UTC)).order_by(IntegrationOAuthAttempt.created_at.desc())).first()
            if connection:
                source=session.exec(select(IntegrationSource).where(IntegrationSource.connection_id==connection.id,IntegrationSource.active==True)).one_or_none()
                last=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id).order_by(IntegrationSyncRun.created_at.desc())).first()
                last_success=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id,IntegrationSyncRun.status.in_(("completed","partial"))).order_by(IntegrationSyncRun.completed_at.desc(),IntegrationSyncRun.id.desc())).first()
                recent_runs=list(session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id).order_by(IntegrationSyncRun.created_at.desc(),IntegrationSyncRun.id.desc()).limit(10)).all())
                schedule=session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection.id)).one_or_none()
            result[provider]={"connection":connection,"source":source,"last":last,"last_success":last_success,"recent_runs":recent_runs,"schedule":schedule,"attempt":attempt}
        return result


def disconnect(engine: Engine, site_id: int, provider: str, *, expected_revision: int | None = None) -> bool:
    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        connection=session.exec(select(IntegrationConnection).where(IntegrationConnection.site_id==site_id,IntegrationConnection.provider==provider)).one_or_none()
        if not connection:return False
        site=session.get(Site,site_id)
        if not site or site.site_type != SITE_TYPE_OWNED:return False
        if expected_revision is not None and connection.revision != expected_revision:return False
        connection.access_token_encrypted=None; connection.refresh_token_encrypted=None; connection.token_expires_at=None; connection.status="disconnected"; connection.last_error=None
        connection.revision += 1; connection.updated_at=datetime.now(UTC)
        session.exec(update(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection.id).values(enabled=False,next_run_at=None))
        session.exec(update(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id,IntegrationSyncRun.status=="pending").values(status="cancelled",completed_at=datetime.now(UTC),message="Запуск отменён после отключения."))
        session.add(connection); session.commit(); return True


def save_integration_schedule(engine: Engine, connection_id: int, *, expected_revision: int,
                              enabled: bool, frequency: str, local_weekday: int,
                              local_time: str, next_run_at: datetime | None) -> IntegrationSchedule:
    """Сохранить расписание только для неизменившегося активного подключения."""

    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        connection=session.get(IntegrationConnection,connection_id)
        site=session.get(Site,connection.site_id) if connection else None
        source=session.exec(select(IntegrationSource).where(IntegrationSource.connection_id==connection_id,IntegrationSource.active==True)).one_or_none() if connection else None
        if not connection or connection.status != "connected" or connection.revision != expected_revision or not site or site.site_type != SITE_TYPE_OWNED or not source:
            raise IntegrationError("Подключение изменилось. Обновите страницу.","connection_changed")
        schedule=session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection_id)).one_or_none() or IntegrationSchedule(connection_id=connection_id)
        schedule.enabled=enabled;schedule.frequency=frequency;schedule.local_weekday=local_weekday;schedule.local_time=local_time;schedule.next_run_at=next_run_at if enabled else None
        session.add(schedule);session.commit();session.refresh(schedule);return schedule


def count_site_data(session: Session, site_id: int) -> int:
    connection_ids=select(IntegrationConnection.id).where(IntegrationConnection.site_id==site_id)
    source_ids=select(IntegrationSource.id).where(IntegrationSource.connection_id.in_(connection_ids))
    return sum(session.exec(select(func.count()).select_from(model).where(column.in_(connection_ids))).one() for model,column in ((IntegrationSource,IntegrationSource.connection_id),(IntegrationSchedule,IntegrationSchedule.connection_id),(IntegrationSyncRun,IntegrationSyncRun.connection_id))) + session.exec(select(func.count()).select_from(IntegrationPageMetric).where(IntegrationPageMetric.source_id.in_(source_ids))).one() + session.exec(select(func.count()).select_from(IntegrationConnection).where(IntegrationConnection.site_id==site_id)).one() + session.exec(select(func.count()).select_from(IntegrationOAuthAttempt).where(IntegrationOAuthAttempt.site_id==site_id)).one()


def delete_site_data(session: Session, site_id: int) -> None:
    connection_ids=select(IntegrationConnection.id).where(IntegrationConnection.site_id==site_id); source_ids=select(IntegrationSource.id).where(IntegrationSource.connection_id.in_(connection_ids))
    session.exec(delete(IntegrationPageMetric).where(IntegrationPageMetric.source_id.in_(source_ids)))
    session.exec(delete(IntegrationSyncRun).where(IntegrationSyncRun.connection_id.in_(connection_ids)))
    session.exec(delete(IntegrationSchedule).where(IntegrationSchedule.connection_id.in_(connection_ids)))
    session.exec(delete(IntegrationSource).where(IntegrationSource.connection_id.in_(connection_ids)))
    session.exec(delete(IntegrationOAuthAttempt).where(IntegrationOAuthAttempt.site_id==site_id))
    session.exec(delete(IntegrationConnection).where(IntegrationConnection.site_id==site_id))


async def refresh_connection(
    engine: Engine,
    box: SecretBox,
    settings,
    connection_id: int,
    client: httpx.AsyncClient,
    token_locks: TokenLocks,
    *,
    force: bool = False,
    previous_access: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> str:
    """Обновить пару токенов один раз и сохранить её атомарно."""

    moment = now or datetime.now(UTC)
    lock = await token_locks.get(connection_id)
    async with lock:
        with Session(engine) as session:
            connection = session.get(IntegrationConnection, connection_id)
            if not connection or connection.status != "connected":
                raise IntegrationError("Подключение изменилось.", "connection_changed")
            if expected_revision is not None and connection.revision != expected_revision:
                raise IntegrationError("Подключение изменилось.", "connection_changed")
            if not connection.access_token_encrypted or not connection.refresh_token_encrypted:
                raise IntegrationError("Требуется повторное подключение.", "invalid_grant")
            current_access = box.decrypt(connection.access_token_encrypted)
            if previous_access is not None and current_access != previous_access:
                return current_access
            if not force and connection.token_expires_at and connection.token_expires_at > moment + timedelta(minutes=2):
                return current_access
            provider = connection.provider
            refresh = box.decrypt(connection.refresh_token_encrypted)
            revision = connection.revision
        config = provider_config(settings, provider)
        url = "https://oauth2.googleapis.com/token" if provider == GOOGLE else "https://oauth.yandex.com/token"
        try:
            payload = await safe_json(
                client,
                "POST",
                url,
                data={"grant_type": "refresh_token", "refresh_token": refresh,
                      "client_id": config.client_id, "client_secret": config.client_secret},
            )
        except IntegrationError as exc:
            if exc.code == "invalid_grant":
                with Session(engine) as session:
                    if engine.dialect.name == "sqlite":
                        session.exec(text("BEGIN IMMEDIATE"))
                    connection = session.get(IntegrationConnection, connection_id)
                    if connection and connection.status == "connected" and connection.revision == revision:
                        connection.status = "reauthorization_required"
                        connection.last_error = str(exc)
                        connection.revision += 1
                        connection.updated_at = moment
                        session.add(connection)
                        session.commit()
            raise
        access = payload.get("access_token")
        rotated = payload.get("refresh_token", refresh)
        expires = payload.get("expires_in", 3600)
        if not isinstance(access, str) or not access or not isinstance(rotated, str) or not rotated:
            raise IntegrationError("Провайдер вернул неполную пару токенов.", "invalid_response")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
            raise IntegrationError("Провайдер вернул неверный срок токена.", "invalid_response")
        with Session(engine) as session:
            if engine.dialect.name == "sqlite":
                session.exec(text("BEGIN IMMEDIATE"))
            connection = session.get(IntegrationConnection, connection_id)
            site = session.get(Site, connection.site_id) if connection else None
            if not connection or connection.status != "connected" or connection.revision != revision or not site or site.site_type != SITE_TYPE_OWNED:
                raise IntegrationError("Подключение изменилось во время обновления токена.", "connection_changed")
            connection.access_token_encrypted = box.encrypt(access)
            connection.refresh_token_encrypted = box.encrypt(rotated)
            connection.token_expires_at = moment + timedelta(seconds=min(int(expires), 31_536_000))
            connection.updated_at = moment
            session.add(connection)
            session.commit()
        return access


async def authenticated_json(
    engine: Engine,
    box: SecretBox,
    settings,
    connection_id: int,
    client: httpx.AsyncClient,
    token_locks: TokenLocks,
    method: str,
    url: str,
    *,
    now: datetime,
    **kwargs,
) -> dict:
    with Session(engine) as session:
        connection = session.get(IntegrationConnection, connection_id)
        if not connection or not connection.access_token_encrypted:
            raise IntegrationError("Требуется повторное подключение.", "invalid_grant")
        access = box.decrypt(connection.access_token_encrypted)
        expires = connection.token_expires_at
    if expires is None or expires <= now + timedelta(minutes=2):
        access = await refresh_connection(engine, box, settings, connection_id, client, token_locks, now=now)
    try:
        return await safe_json(client, method, url, headers={"Authorization": f"Bearer {access}"}, **kwargs)
    except IntegrationError as exc:
        if exc.code != "unauthorized":
            raise
    access = await refresh_connection(
        engine, box, settings, connection_id, client, token_locks,
        force=True, previous_access=access, now=now,
    )
    return await safe_json(client, method, url, headers={"Authorization": f"Bearer {access}"}, **kwargs)


def _integer(value, name:str) -> int | None:
    if value is None:return None
    if isinstance(value,bool):raise IntegrationError(f"Некорректное поле {name}.","invalid_response")
    try:
        parsed=Decimal(str(value))
        if parsed<0 or parsed!=parsed.to_integral_value():raise ValueError
        return int(parsed)
    except (InvalidOperation,ValueError):raise IntegrationError(f"Некорректное поле {name}.","invalid_response")


def _position(value) -> str | None:
    if value is None:return None
    try:
        parsed=Decimal(str(value))
        if not parsed.is_finite() or parsed<0:raise ValueError
        return format(parsed.normalize(),"f")
    except (InvalidOperation,ValueError):raise IntegrationError("Некорректная позиция.","invalid_response")


def _validated_metric(site_url:str, day, page, clicks, impressions, position) -> tuple[date,str,int|None,int|None,str|None]:
    try:d=date.fromisoformat(str(day))
    except ValueError as exc:raise IntegrationError("Некорректная дата показателя.","invalid_response") from exc
    if not isinstance(page,str) or len(page)>2048:raise IntegrationError("Некорректный URL показателя.","invalid_response")
    normalized=normalize_http_url(page); own=normalize_http_url(site_url)
    if not normalized or not own or _host(normalized)!=_host(own):raise IntegrationError("Провайдер вернул URL другого домена.","external_url")
    parsed_clicks=_integer(clicks,"клики");parsed_impressions=_integer(impressions,"показы")
    if parsed_clicks is not None and parsed_impressions is not None and parsed_clicks>parsed_impressions:
        raise IntegrationError("Клики не могут превышать показы.","invalid_response")
    return d,normalized,parsed_clicks,parsed_impressions,_position(position)


def _parse_yandex_page(data: dict, site_url: str) -> tuple[int, list[tuple[date,str,int|None,int|None,str|None]]]:
    count = data.get("count")
    batch = data.get("text_indicator_to_statistics")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(batch, list):
        raise IntegrationError("Яндекс вернул некорректную страницу статистики.", "invalid_response")
    parsed: list[tuple[date,str,int|None,int|None,str|None]] = []
    for item in batch:
        if not isinstance(item, dict):
            raise IntegrationError("Яндекс вернул некорректную строку статистики.", "invalid_response")
        indicator = item.get("text_indicator")
        statistics = item.get("statistics")
        if not isinstance(indicator, dict) or indicator.get("type") != "URL" or not isinstance(statistics, list):
            raise IntegrationError("Яндекс вернул неверный URL-индикатор.", "invalid_response")
        page = indicator.get("value")
        by_date: dict[str, dict[str, object]] = {}
        for statistic in statistics:
            if not isinstance(statistic, dict):
                raise IntegrationError("Яндекс вернул некорректный показатель.", "invalid_response")
            day, field, value = statistic.get("date"), statistic.get("field"), statistic.get("value")
            if not isinstance(day, str) or field not in {"CLICKS", "IMPRESSIONS", "POSITION", "CTR"}:
                raise IntegrationError("Яндекс вернул неизвестный показатель.", "invalid_response")
            fields = by_date.setdefault(day, {})
            if field in fields:
                raise IntegrationError("Яндекс повторил показатель одной даты.", "invalid_response")
            fields[field] = value
        for day, fields in by_date.items():
            parsed.append(_validated_metric(site_url, day, page, fields.get("CLICKS"), fields.get("IMPRESSIONS"), fields.get("POSITION")))
    return count, parsed


def _finish_run_error(engine: Engine, run_id: int, error: IntegrationError,
                      moment: datetime, expected_revision: int) -> None:
    """Завершить существующий журнал, не воскрешая удалённое состояние."""

    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        run=session.get(IntegrationSyncRun,run_id)
        if run is None:
            session.rollback();return
        connection=session.get(IntegrationConnection,run.connection_id)
        if connection is None:
            session.rollback();return
        changed=connection.status != "connected" or connection.revision != expected_revision
        run.status="interrupted" if changed else "failed"
        run.completed_at=moment;run.error_code=error.code;run.message=(
            "Синхронизация остановлена: подключение изменилось во время внешнего запроса."
            if changed else str(error)
        )
        if not changed and error.code in {"invalid_grant","decrypt_failed"}:
            connection.status="reauthorization_required";connection.last_error=str(error);connection.revision += 1;connection.updated_at=moment;session.add(connection)
        session.add(run);session.commit()


async def execute_pending(
    engine:Engine, box:SecretBox, settings, client:httpx.AsyncClient,
    token_locks: TokenLocks | None = None, *, now: datetime | None = None,
) -> bool:
    """Выполнить один сохранённый запуск; все строки фиксируются одной транзакцией."""
    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            session.exec(text("BEGIN IMMEDIATE"))
        moment=now or datetime.now(UTC)
        due=list(session.exec(select(IntegrationSchedule).where(IntegrationSchedule.enabled==True,IntegrationSchedule.next_run_at!=None,IntegrationSchedule.next_run_at<=moment)).all())
        for schedule in due:
            connection=session.get(IntegrationConnection,schedule.connection_id)
            source=session.exec(select(IntegrationSource).where(IntegrationSource.connection_id==schedule.connection_id,IntegrationSource.active==True)).one_or_none()
            active=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==schedule.connection_id,IntegrationSyncRun.status.in_(("pending","running")))).first()
            site=session.get(Site,connection.site_id) if connection else None
            if connection and connection.status=="connected" and source and source.active and site and site.site_type==SITE_TYPE_OWNED and active is None:session.add(IntegrationSyncRun(connection_id=connection.id,source_id=source.id,trigger="scheduled"))
            next_run=schedule.next_run_at
            while next_run and next_run<=moment:
                next_run=advance_run(next_run,frequency=schedule.frequency,local_timezone=settings.local_timezone)
            schedule.next_run_at=next_run;session.add(schedule)
        if due:session.commit()
        run=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.status=="pending").order_by(IntegrationSyncRun.created_at,IntegrationSyncRun.id)).first()
        if not run:return False
        connection=session.get(IntegrationConnection,run.connection_id); source=session.get(IntegrationSource,run.source_id) if run.source_id else None
        site=session.get(Site,connection.site_id) if connection else None
        if not connection or not source or not site or site.site_type!=SITE_TYPE_OWNED:
            run.status="cancelled";run.message="Запуск отменён: собственный сайт или источник недоступен.";run.completed_at=moment;session.add(run);session.commit();return True
        competing=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id,IntegrationSyncRun.status=="running")).first()
        if competing:return False
        end=(moment-timedelta(days=2)).date(); start=end-timedelta(days=13 if connection.provider==YANDEX else 30)
        run.status="running";run.started_at=moment;run.requested_start=start;run.requested_end=end;session.add(run);session.commit(); run_id=run.id
        provider=connection.provider; resource=source.resource_id; source_id=source.id; user_id=connection.provider_user_id; site_url=site.url; connection_id=connection.id; connection_revision=connection.revision
    try:
        locks=token_locks or TokenLocks(); rows=[]; partial=False
        if provider==GOOGLE:
            offset=0
            while offset<MAX_ROWS:
                limit=min(GOOGLE_PAGE_SIZE,MAX_ROWS-offset)
                data=await authenticated_json(engine,box,settings,connection_id,client,locks,"POST",f"https://www.googleapis.com/webmasters/v3/sites/{quote(resource,safe='')}/searchAnalytics/query",now=moment,json={"startDate":start.isoformat(),"endDate":end.isoformat(),"dimensions":["date","page"],"type":"web","dataState":"final","rowLimit":limit,"startRow":offset})
                batch=data.get("rows",[])
                if not isinstance(batch,list) or len(batch)>limit:raise IntegrationError("Некорректный список показателей.","invalid_response")
                for item in batch:
                    keys=item.get("keys",[]) if isinstance(item,dict) else []
                    if len(keys)!=2:raise IntegrationError("Некорректные измерения Search Console.","invalid_response")
                    rows.append(_validated_metric(site_url,keys[0],keys[1],item.get("clicks"),item.get("impressions"),item.get("position")))
                if not batch or len(batch)<limit:break
                offset+=len(batch)
            if len(rows)>=MAX_ROWS:partial=True
        else:
            if not user_id:raise IntegrationError("Неизвестен пользователь Яндекс Вебмастера.","invalid_response")
            offset=0
            while offset<MAX_ROWS:
                limit=min(YANDEX_PAGE_SIZE,MAX_ROWS-offset)
                body={"offset":offset,"limit":limit,"device_type_indicator":"ALL","search_location":"WEB_LOCATION","text_indicator":"URL","filters":{"statistic_filters":[{"statistic_field":"IMPRESSIONS","from":start.isoformat(),"to":end.isoformat()}]}}
                data=await authenticated_json(engine,box,settings,connection_id,client,locks,"POST",f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{quote(resource,safe='')}/query-analytics/list",now=moment,json=body)
                count,batch=_parse_yandex_page(data,site_url)
                raw_batch=data["text_indicator_to_statistics"]
                if len(raw_batch)>limit or count<offset+len(raw_batch):raise IntegrationError("Яндекс вернул неверное количество строк.","invalid_response")
                rows.extend(batch)
                offset+=len(raw_batch)
                if offset>=count:break
                if not raw_batch:raise IntegrationError("Яндекс прервал пагинацию до count.","invalid_response")
            if offset< count or count>MAX_ROWS:partial=True
        keys=[(item[0],item[1]) for item in rows]
        if len(keys)!=len(set(keys)):
            raise IntegrationError("Провайдер повторил URL и дату в одном ответе.","invalid_response")
        with Session(engine) as session:
            if engine.dialect.name == "sqlite":
                session.exec(text("BEGIN IMMEDIATE"))
            run=session.get(IntegrationSyncRun,run_id)
            if run is None:
                session.rollback();return True
            connection=session.get(IntegrationConnection,run.connection_id)
            current=session.get(IntegrationSource,source_id)
            site=session.get(Site,connection.site_id) if connection else None
            if not connection or not current or not current.active or current.connection_id!=connection_id or connection.status!="connected" or connection.revision != connection_revision or not site or site.site_type != SITE_TYPE_OWNED:
                raise IntegrationError("Ресурс или подключение изменились во время синхронизации.","connection_changed")
            existing={(m.metric_date,m.normalized_url):m for m in session.exec(select(IntegrationPageMetric).where(IntegrationPageMetric.source_id==source_id)).all()}; added=updated=unchanged=0
            for day,url,clicks,impressions,position in rows:
                metric=existing.get((day,url))
                if metric is None:metric=IntegrationPageMetric(source_id=source_id,provider=provider,metric_date=day,normalized_url=url,clicks=clicks,impressions=impressions,position_text=position);added+=1
                elif (metric.clicks,metric.impressions,metric.position_text)==(clicks,impressions,position):unchanged+=1;continue
                else:metric.clicks=clicks;metric.impressions=impressions;metric.position_text=position;metric.updated_at=datetime.now(UTC);updated+=1
                session.add(metric)
            run.status="partial" if partial else "completed";run.completed_at=moment;run.actual_start=min((x[0] for x in rows),default=None);run.actual_end=max((x[0] for x in rows),default=None);run.added_count=added;run.updated_count=updated;run.unchanged_count=unchanged;run.message="Данные сохранены; источник может возвращать ограниченный набор строк.";session.add(run);session.commit()
    except IntegrationError as exc:
        _finish_run_error(engine,run_id,exc,moment,connection_revision)
    except SQLAlchemyError:
        with Session(engine) as session:
            if engine.dialect.name == "sqlite":
                session.exec(text("BEGIN IMMEDIATE"))
            run=session.get(IntegrationSyncRun,run_id)
            if run:
                run.status="failed";run.completed_at=moment;run.error_code="storage_error";run.message="Не удалось безопасно сохранить проверенный набор данных.";session.add(run);session.commit()
    return True


def recover_interrupted(engine:Engine) -> None:
    with Session(engine) as session:
        session.exec(update(IntegrationSyncRun).where(IntegrationSyncRun.status=="running").values(status="interrupted",completed_at=datetime.now(UTC),message="Запуск прерван перезапуском приложения."));session.commit()


def validate_connection_secrets(engine: Engine, box: SecretBox) -> None:
    """На старте отметить повреждённые секреты, не удаляя историю и показатели."""

    with Session(engine) as session:
        changed=False
        for connection in session.exec(select(IntegrationConnection).where(IntegrationConnection.status=="connected")).all():
            try:
                if not connection.access_token_encrypted or not connection.refresh_token_encrypted:
                    raise IntegrationError("Защищённые данные отсутствуют.","decrypt_failed")
                box.decrypt(connection.access_token_encrypted);box.decrypt(connection.refresh_token_encrypted)
            except IntegrationError:
                connection.status="reauthorization_required";connection.last_error="Защищённые данные недоступны. Подключите аккаунт повторно.";session.add(connection);changed=True
                connection.revision += 1;connection.updated_at=datetime.now(UTC)
        if changed:session.commit()


def mark_connections_reauthorization(engine: Engine, message: str) -> None:
    with Session(engine) as session:
        session.exec(update(IntegrationConnection).where(IntegrationConnection.status=="connected").values(status="reauthorization_required",last_error=message,updated_at=datetime.now(UTC),revision=IntegrationConnection.revision+1))
        session.commit()
