from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from marketing_intelligence.config import Settings
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.integrations import (GOOGLE, YANDEX, IntegrationError,
    SecretBox, consume_attempt, matching_resources, start_oauth)
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (IntegrationOAuthAttempt,
    IntegrationConnection, Site, SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED)


def settings(tmp_path:Path,monkeypatch,configured=True):
    names={"MI_GOOGLE_CLIENT_ID":"google-id","MI_GOOGLE_CLIENT_SECRET":"google-secret","MI_GOOGLE_REDIRECT_URI":"http://127.0.0.1/oauth/google_search_console/callback","MI_YANDEX_CLIENT_ID":"yandex-id","MI_YANDEX_CLIENT_SECRET":"yandex-secret","MI_YANDEX_REDIRECT_URI":"http://localhost/oauth/yandex_webmaster/callback"}
    for key,value in names.items():monkeypatch.setenv(key,value if configured else "")
    monkeypatch.setenv("MI_DATA_DIR",str(tmp_path/"data"));monkeypatch.setenv("MI_LOGS_DIR",str(tmp_path/"logs"));monkeypatch.setenv("MI_DATABASE_URL",f"sqlite:///{(tmp_path/'app.db').as_posix()}")
    return Settings.from_environment()


def test_configuration_is_optional_and_redirect_is_validated(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch,False)
    assert not active.google_oauth.enabled and not active.yandex_oauth.enabled
    monkeypatch.setenv("MI_GOOGLE_CLIENT_ID","x");monkeypatch.setenv("MI_GOOGLE_CLIENT_SECRET","secret");monkeypatch.setenv("MI_GOOGLE_REDIRECT_URI","http://example.com/callback?code=x")
    assert not Settings.from_environment().google_oauth.enabled
    assert "HTTP разрешён" in Settings.from_environment().google_oauth.error
    assert "secret" not in repr(Settings.from_environment())


def test_key_is_persistent_private_and_ciphertext_contains_no_secret(tmp_path):
    box=SecretBox(tmp_path);encrypted=box.encrypt("very-secret-token")
    assert "very-secret-token" not in encrypted and SecretBox(tmp_path).decrypt(encrypted)=="very-secret-token"
    assert (tmp_path/"integration.key").read_bytes()!=b"very-secret-token"


def test_schema_is_idempotent_and_keeps_old_site(tmp_path):
    engine=build_engine(f"sqlite:///{(tmp_path/'old.db').as_posix()}");initialize_database(engine)
    with Session(engine) as session:session.add(Site(id=7,name="Сайт",url="https://example.test",site_type=SITE_TYPE_OWNED));session.commit()
    initialize_database(engine)
    with Session(engine) as session:
        assert session.get(Site,7).name=="Сайт"
        assert session.exec(select(IntegrationConnection)).all()==[]


def test_oauth_attempt_is_persistent_pkce_and_one_time(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);engine=build_engine(active.database_url);initialize_database(engine);box=SecretBox(active.data_dir)
    with Session(engine) as session:session.add(Site(id=1,name="Сайт",url="https://пример.рф",site_type=SITE_TYPE_OWNED));session.commit()
    url=start_oauth(engine,box,1,YANDEX,active.yandex_oauth);query=parse_qs(urlsplit(url).query);state=query["state"][0]
    assert query["scope"]==["webmaster:read"] and query["code_challenge_method"]==["S256"]
    attempt,verifier=consume_attempt(engine,box,YANDEX,state)
    assert attempt.used_at and verifier and verifier not in (tmp_path/"app.db").read_bytes().decode("latin1")
    try:consume_attempt(engine,box,YANDEX,state)
    except IntegrationError as exc:assert exc.code=="invalid_state"
    else:assert False


def test_google_scope_offline_and_owned_only(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);engine=build_engine(active.database_url);initialize_database(engine);box=SecretBox(active.data_dir)
    with Session(engine) as session:session.add(Site(id=1,name="Конкурент",url="https://example.test",site_type=SITE_TYPE_COMPETITOR));session.commit()
    try:start_oauth(engine,box,1,GOOGLE,active.google_oauth)
    except IntegrationError:pass
    else:assert False
    with Session(engine) as session:site=session.get(Site,1);site.site_type=SITE_TYPE_OWNED;session.add(site);session.commit()
    query=parse_qs(urlsplit(start_oauth(engine,box,1,GOOGLE,active.google_oauth)).query)
    assert query["scope"]==["https://www.googleapis.com/auth/webmasters.readonly"] and query["access_type"]==["offline"]


def test_resource_matching_handles_domain_idna_www_and_ambiguity():
    google=[{"siteUrl":"sc-domain:XN--E1AFMKFD.XN--P1AI.","permissionLevel":"siteOwner"},{"siteUrl":"https://www.пример.рф:443/","permissionLevel":"siteFullUser"}]
    matches=matching_resources("https://пример.рф",GOOGLE,google)
    assert len(matches)==2
    yandex=[{"host_id":"h1","unicode_host_url":"https://www.пример.рф","verified":True,"main_mirror":"https://пример.рф"}]
    assert matching_resources("https://пример.рф",YANDEX,yandex)==[{"id":"h1","label":"https://www.пример.рф"}]


def test_integration_screen_is_owned_only_read_only_and_no_credentials(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch,False);app=create_app(active)
    with TestClient(app) as client:
        client.post("/own-sites",data={"name":"Свой","url":"https://own.test"});client.post("/competitors",data={"name":"Чужой","url":"https://other.test"})
        before=sha256((tmp_path/"app.db").read_bytes()).hexdigest();response=client.get("/own-sites/1/integrations");after=sha256((tmp_path/"app.db").read_bytes()).hexdigest()
        assert response.status_code==200 and "Google Search Console" in response.text and "Яндекс Вебмастер" in response.text and "Не настроено" in response.text
        assert before==after and client.get("/own-sites/2/integrations").status_code==404


def test_connect_is_protected_post_and_secrets_never_render(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);app=create_app(active)
    with TestClient(app) as client:
        client.post("/own-sites",data={"name":"Свой","url":"https://own.test"})
        assert client.get("/own-sites/1/integrations/google_search_console/connect").status_code==405
        page=client.get("/own-sites/1/integrations");assert "google-secret" not in page.text
        forbidden=client.post("/own-sites/1/integrations/google_search_console/connect",data={"action_token":"bad"},follow_redirects=False)
        assert forbidden.status_code==303
