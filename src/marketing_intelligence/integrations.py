"""Безопасная общая основа read-only поисковых интеграций."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import os
from pathlib import Path
import secrets
from urllib.parse import quote, urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
import httpx
from sqlalchemy import delete, func, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .config import OAuthProviderConfig
from .models import (IntegrationConnection, IntegrationOAuthAttempt,
    IntegrationPageMetric, IntegrationSchedule, IntegrationSource,
    IntegrationSyncRun, Site, SITE_TYPE_OWNED)
from .link_discovery import normalize_http_url, url_origin

GOOGLE = "google_search_console"
YANDEX = "yandex_webmaster"
PROVIDERS = (GOOGLE, YANDEX)
GOOGLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
YANDEX_SCOPE = "webmaster:read"
MAX_RESPONSE = 2 * 1024 * 1024
MAX_ROWS = 10_000


class IntegrationError(RuntimeError):
    def __init__(self, message: str, code: str = "integration_error") -> None:
        super().__init__(message); self.code = code


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
    if response.status_code == 429 or response.status_code >= 500: raise IntegrationError("Внешний сервис временно ограничил запрос.", "retryable")
    if response.status_code >= 400: raise IntegrationError("Внешний сервис отклонил запрос.", "provider_error")
    if len(response.content)>MAX_RESPONSE or "json" not in response.headers.get("content-type","").lower(): raise IntegrationError("Внешний сервис вернул недопустимый ответ.", "invalid_response")
    try: data=response.json()
    except ValueError as exc: raise IntegrationError("Внешний сервис вернул некорректные данные.", "invalid_json") from exc
    if not isinstance(data,dict): raise IntegrationError("Внешний сервис вернул некорректную структуру.", "invalid_json")
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
        connection=session.exec(select(IntegrationConnection).where(IntegrationConnection.site_id==attempt.site_id,IntegrationConnection.provider==provider)).one_or_none()
        if connection is None: connection=IntegrationConnection(site_id=attempt.site_id,provider=provider)
        connection.status="connected"; connection.access_token_encrypted=box.encrypt(access); connection.refresh_token_encrypted=box.encrypt(refresh)
        connection.token_expires_at=datetime.now(UTC)+timedelta(seconds=min(int(expires),86400)); connection.last_error=None; connection.updated_at=datetime.now(UTC)
        session.add(connection); session.commit(); session.refresh(connection); return connection.id or 0


def _host(value: str) -> str | None:
    raw=value[10:] if value.lower().startswith("sc-domain:") else value
    try: host=(urlsplit(raw if "://" in raw else "https://"+raw).hostname or "").rstrip(".").lower().encode("idna").decode()
    except (ValueError,UnicodeError): return None
    return host[4:] if host.startswith("www.") else host


def matching_resources(site_url: str, provider: str, resources: list[dict]) -> list[dict]:
    target=_host(site_url); matches=[]
    for resource in resources:
        rid=resource.get("id") or resource.get("siteUrl") or resource.get("host_id")
        label=resource.get("label") or resource.get("unicode_host_url") or resource.get("ascii_host_url") or rid
        permission=str(resource.get("permissionLevel") or resource.get("verification") or resource.get("verified") or "").lower()
        if not isinstance(rid,str) or not isinstance(label,str): continue
        if provider==GOOGLE and permission in {"siteunverifieduser","none"}: continue
        if provider==YANDEX and permission in {"false","unverified","none"}: continue
        candidates=[rid,label,str(resource.get("ascii_host_url") or ""),str(resource.get("main_mirror") or "")]
        if target and any(_host(v)==target for v in candidates if v): matches.append({"id":rid,"label":label})
    return matches


def automatic_resource(site_url:str,provider:str,resources:list[dict]) -> dict | None:
    if not resources:return None
    if provider==GOOGLE:
        own=normalize_http_url(site_url); own_origin=url_origin(own) if own else None
        exact=[];domain=[]
        for resource in resources:
            rid=resource["id"]
            normalized=normalize_http_url(rid)
            if normalized and url_origin(normalized)==own_origin:exact.append(resource)
            elif rid.lower().startswith("sc-domain:"):domain.append(resource)
        best=exact or domain
    else:best=resources
    return best[0] if len(best)==1 else None


def select_resource(engine: Engine, connection_id: int, resource: dict) -> IntegrationSource:
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,connection_id)
        if not connection: raise IntegrationError("Подключение не найдено.","not_found")
        session.exec(update(IntegrationSource).where(IntegrationSource.connection_id==connection_id,IntegrationSource.active==True).values(active=False))
        version=(session.exec(select(func.max(IntegrationSource.version)).where(IntegrationSource.connection_id==connection_id)).one() or 0)+1
        source=IntegrationSource(connection_id=connection_id,provider=connection.provider,version=version,resource_id=resource["id"],resource_label=resource["label"])
        session.add(source); session.flush()
        if session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection_id)).one_or_none() is None:
            session.add(IntegrationSchedule(connection_id=connection_id))
        session.add(IntegrationSyncRun(connection_id=connection_id,source_id=source.id,trigger="initial")); session.commit(); session.refresh(source); return source


async def available_resources(engine: Engine, box: SecretBox, connection_id: int,
                              client: httpx.AsyncClient, *, persist_user: bool = False) -> list[dict]:
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,connection_id)
        if not connection or not connection.access_token_encrypted: raise IntegrationError("Подключение не найдено.","not_found")
        site=session.get(Site,connection.site_id)
        try: token=box.decrypt(connection.access_token_encrypted)
        except IntegrationError:
            connection.status="reauthorization_required"; connection.last_error="Защищённые данные недоступны. Подключите аккаунт повторно."; session.add(connection); session.commit(); raise
        provider=connection.provider
    headers={"Authorization":f"Bearer {token}"}
    if provider==GOOGLE:
        data=await safe_json(client,"GET","https://www.googleapis.com/webmasters/v3/sites",headers=headers)
        raw=data.get("siteEntry",[])
    else:
        user=await safe_json(client,"GET","https://api.webmaster.yandex.net/v4/user",headers=headers)
        uid=user.get("user_id")
        if not isinstance(uid,(str,int)): raise IntegrationError("Яндекс не вернул идентификатор пользователя.","invalid_response")
        data=await safe_json(client,"GET",f"https://api.webmaster.yandex.net/v4/user/{uid}/hosts",headers=headers)
        raw=data.get("hosts",[])
        if persist_user:
            with Session(engine) as session:
                connection=session.get(IntegrationConnection,connection_id)
                if connection: connection.provider_user_id=str(uid); session.add(connection); session.commit()
    if not isinstance(raw,list) or len(raw)>MAX_ROWS: raise IntegrationError("Список ресурсов имеет недопустимый размер.","invalid_response")
    return matching_resources(site.url,provider,[x for x in raw if isinstance(x,dict)])


def snapshot(engine: Engine, site_id: int) -> dict[str, dict]:
    with Session(engine) as session:
        result={}
        for provider in PROVIDERS:
            connection=session.exec(select(IntegrationConnection).where(IntegrationConnection.site_id==site_id,IntegrationConnection.provider==provider)).one_or_none()
            source=None; last=None; schedule=None
            if connection:
                source=session.exec(select(IntegrationSource).where(IntegrationSource.connection_id==connection.id,IntegrationSource.active==True)).one_or_none()
                last=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id).order_by(IntegrationSyncRun.created_at.desc())).first()
                schedule=session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection.id)).one_or_none()
            result[provider]={"connection":connection,"source":source,"last":last,"schedule":schedule}
        return result


def disconnect(engine: Engine, site_id: int, provider: str) -> bool:
    with Session(engine) as session:
        connection=session.exec(select(IntegrationConnection).where(IntegrationConnection.site_id==site_id,IntegrationConnection.provider==provider)).one_or_none()
        if not connection:return False
        connection.access_token_encrypted=None; connection.refresh_token_encrypted=None; connection.token_expires_at=None; connection.status="disconnected"; connection.last_error=None
        session.exec(update(IntegrationSchedule).where(IntegrationSchedule.connection_id==connection.id).values(enabled=False,next_run_at=None))
        session.exec(update(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id,IntegrationSyncRun.status=="pending").values(status="cancelled",message="Запуск отменён после отключения."))
        session.add(connection); session.commit(); return True


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


async def _refresh(provider: str, refresh_encrypted: str | None, box: SecretBox, config: OAuthProviderConfig,
                   client: httpx.AsyncClient) -> tuple[str,str]:
    if not refresh_encrypted: raise IntegrationError("Требуется повторное подключение.","invalid_grant")
    refresh=box.decrypt(refresh_encrypted)
    url="https://oauth2.googleapis.com/token" if provider==GOOGLE else "https://oauth.yandex.com/token"
    data=await safe_json(client,"POST",url,data={"grant_type":"refresh_token","refresh_token":refresh,"client_id":config.client_id,"client_secret":config.client_secret})
    access=data.get("access_token"); rotated=data.get("refresh_token",refresh)
    if not isinstance(access,str) or not access or not isinstance(rotated,str) or not rotated: raise IntegrationError("Требуется повторное подключение.","invalid_grant")
    return access,rotated


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
    return d,normalized,_integer(clicks,"клики"),_integer(impressions,"показы"),_position(position)


async def execute_pending(engine:Engine,box:SecretBox,settings,client:httpx.AsyncClient) -> bool:
    """Выполнить один сохранённый запуск; все строки фиксируются одной транзакцией."""
    with Session(engine) as session:
        now=datetime.now(UTC)
        due=list(session.exec(select(IntegrationSchedule).where(IntegrationSchedule.enabled==True,IntegrationSchedule.next_run_at!=None,IntegrationSchedule.next_run_at<=now)).all())
        for schedule in due:
            connection=session.get(IntegrationConnection,schedule.connection_id)
            source=session.exec(select(IntegrationSource).where(IntegrationSource.connection_id==schedule.connection_id,IntegrationSource.active==True)).one_or_none()
            active=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==schedule.connection_id,IntegrationSyncRun.status.in_(("pending","running")))).first()
            if connection and source and active is None:session.add(IntegrationSyncRun(connection_id=connection.id,source_id=source.id,trigger="scheduled"))
            schedule.next_run_at=now+timedelta(days=1 if schedule.frequency=="daily" else 7);session.add(schedule)
        if due:session.commit()
        run=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.status=="pending").order_by(IntegrationSyncRun.created_at,IntegrationSyncRun.id)).first()
        if not run:return False
        connection=session.get(IntegrationConnection,run.connection_id); source=session.get(IntegrationSource,run.source_id) if run.source_id else None
        site=session.get(Site,connection.site_id) if connection else None
        if not connection or not source or not site or site.site_type!=SITE_TYPE_OWNED:
            run.status="cancelled";run.message="Запуск отменён: собственный сайт или источник недоступен.";run.completed_at=datetime.now(UTC);session.add(run);session.commit();return True
        competing=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==connection.id,IntegrationSyncRun.status=="running")).first()
        if competing:return False
        run.status="running";run.started_at=datetime.now(UTC);session.add(run);session.commit(); run_id=run.id
        access_enc=connection.access_token_encrypted; refresh_enc=connection.refresh_token_encrypted; expires=connection.token_expires_at; provider=connection.provider; resource=source.resource_id; source_id=source.id; user_id=connection.provider_user_id; site_url=site.url
    try:
        if not access_enc:raise IntegrationError("Требуется повторное подключение.","invalid_grant")
        access=box.decrypt(access_enc); rotated=None
        if expires is None or expires<=datetime.now(UTC)+timedelta(minutes=2):
            access,rotated=await _refresh(provider,refresh_enc,box,provider_config(settings,provider),client)
        headers={"Authorization":f"Bearer {access}"}; end=date.today()-timedelta(days=2); start=end-timedelta(days=15 if provider==YANDEX else 30)
        rows=[]; partial=False
        if provider==GOOGLE:
            offset=0
            while offset<MAX_ROWS:
                data=await safe_json(client,"POST",f"https://www.googleapis.com/webmasters/v3/sites/{quote(resource,safe='')}/searchAnalytics/query",headers=headers,json={"startDate":start.isoformat(),"endDate":end.isoformat(),"dimensions":["date","page"],"type":"web","dataState":"final","rowLimit":25000,"startRow":offset})
                batch=data.get("rows",[])
                if not isinstance(batch,list):raise IntegrationError("Некорректный список показателей.","invalid_response")
                for item in batch:
                    keys=item.get("keys",[]) if isinstance(item,dict) else []
                    if len(keys)!=2:raise IntegrationError("Некорректные измерения Search Console.","invalid_response")
                    rows.append(_validated_metric(site_url,keys[0],keys[1],item.get("clicks"),item.get("impressions"),item.get("position")))
                    if len(rows)>=MAX_ROWS:partial=True;break
                if len(batch)<25000 or partial:break
                offset+=len(batch)
        else:
            if not user_id:raise IntegrationError("Неизвестен пользователь Яндекс Вебмастера.","invalid_response")
            offset=0
            while offset<MAX_ROWS:
                data=await safe_json(client,"POST",f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{resource}/query-analytics/list",headers=headers,json={"date_from":start.isoformat(),"date_to":end.isoformat(),"text_indicator":"URL","device_type_indicator":"ALL","offset":offset,"limit":500})
                batch=data.get("queries",data.get("rows",[]))
                if not isinstance(batch,list):raise IntegrationError("Некорректный список показателей.","invalid_response")
                for item in batch:
                    if not isinstance(item,dict):raise IntegrationError("Некорректная строка показателей.","invalid_response")
                    rows.append(_validated_metric(site_url,item.get("date"),item.get("url") or item.get("text"),item.get("clicks"),item.get("shows",item.get("impressions")),item.get("position")))
                    if len(rows)>=MAX_ROWS:partial=True;break
                if len(batch)<500 or partial:break
                offset+=len(batch)
        with Session(engine) as session:
            run=session.get(IntegrationSyncRun,run_id); connection=session.get(IntegrationConnection,run.connection_id)
            existing={(m.metric_date,m.normalized_url):m for m in session.exec(select(IntegrationPageMetric).where(IntegrationPageMetric.source_id==source_id)).all()}; added=updated=unchanged=0
            for day,url,clicks,impressions,position in rows:
                metric=existing.get((day,url))
                if metric is None:metric=IntegrationPageMetric(source_id=source_id,provider=provider,metric_date=day,normalized_url=url,clicks=clicks,impressions=impressions,position_text=position);added+=1
                elif (metric.clicks,metric.impressions,metric.position_text)==(clicks,impressions,position):unchanged+=1;continue
                else:metric.clicks=clicks;metric.impressions=impressions;metric.position_text=position;metric.updated_at=datetime.now(UTC);updated+=1
                session.add(metric)
            if rotated:
                connection.access_token_encrypted=box.encrypt(access);connection.refresh_token_encrypted=box.encrypt(rotated);connection.token_expires_at=datetime.now(UTC)+timedelta(hours=1);session.add(connection)
            run.status="partial" if partial else "completed";run.completed_at=datetime.now(UTC);run.actual_start=min((x[0] for x in rows),default=None);run.actual_end=max((x[0] for x in rows),default=None);run.added_count=added;run.updated_count=updated;run.unchanged_count=unchanged;run.message="Данные сохранены; источник может возвращать ограниченный набор строк.";session.add(run);session.commit()
    except IntegrationError as exc:
        with Session(engine) as session:
            run=session.get(IntegrationSyncRun,run_id); connection=session.get(IntegrationConnection,run.connection_id);run.status="failed";run.completed_at=datetime.now(UTC);run.error_code=exc.code;run.message=str(exc)
            if exc.code in {"invalid_grant","decrypt_failed"}:connection.status="reauthorization_required";connection.last_error=str(exc);session.add(connection)
            session.add(run);session.commit()
    return True


def recover_interrupted(engine:Engine) -> None:
    with Session(engine) as session:
        session.exec(update(IntegrationSyncRun).where(IntegrationSyncRun.status=="running").values(status="interrupted",completed_at=datetime.now(UTC),message="Запуск прерван перезапуском приложения."));session.commit()
