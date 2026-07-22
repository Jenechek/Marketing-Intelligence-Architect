import asyncio
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from marketing_intelligence.config import Settings
from marketing_intelligence.database import build_engine, initialize_database
from marketing_intelligence.integrations import (GOOGLE, YANDEX, IntegrationError,
    SecretBox, TokenLocks, consume_attempt, disconnect, enqueue_sync, execute_pending,
    finish_oauth, matching_resources, safe_json, select_resource, start_oauth)
from marketing_intelligence.main import create_app
from marketing_intelligence.models import (IntegrationOAuthAttempt,
    IntegrationConnection, IntegrationPageMetric, IntegrationSchedule, IntegrationSource,
    IntegrationSyncRun, Site, SITE_TYPE_COMPETITOR, SITE_TYPE_OWNED)
from marketing_intelligence.sites import delete_site


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


def test_expired_resource_get_is_read_only_and_offers_protected_refresh(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);requests=[]
    def provider(request):
        requests.append(request)
        return httpx.Response(500,json={"error":"must-not-be-called"})
    app=create_app(active,integration_transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        client.post("/own-sites",data={"name":"Свой","url":"https://example.test"})
        box=app.state.integration_box
        with Session(app.state.engine) as session:
            session.add(IntegrationConnection(id=1,site_id=1,provider=GOOGLE,status="connected",access_token_encrypted=box.encrypt("expired"),refresh_token_encrypted=box.encrypt("refresh"),token_expires_at=datetime.now(UTC)-timedelta(seconds=1)))
            session.commit()
        before=sha256((tmp_path/"app.db").read_bytes()).hexdigest()
        response=client.get("/own-sites/1/integrations/google_search_console/resources")
        after=sha256((tmp_path/"app.db").read_bytes()).hexdigest()
        assert response.status_code==200 and before==after and requests==[]
        form=BeautifulSoup(response.text,"html.parser").find("form",attrs={"action":"/own-sites/1/integrations/google_search_console/resources/refresh"})
        assert form is not None and form.find("input",attrs={"name":"action_token"})


def test_protected_resource_refresh_rotates_tokens_then_opens_current_list(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);calls=[]
    def provider(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200,json={"access_token":"fresh","refresh_token":"rotated","expires_in":3600})
        return httpx.Response(200,json={"siteEntry":[{"siteUrl":"sc-domain:example.test","permissionLevel":"siteOwner"}]})
    app=create_app(active,integration_transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        client.post("/own-sites",data={"name":"Свой","url":"https://example.test"})
        box=app.state.integration_box
        with Session(app.state.engine) as session:
            session.add(IntegrationConnection(id=1,site_id=1,provider=GOOGLE,status="connected",access_token_encrypted=box.encrypt("expired"),refresh_token_encrypted=box.encrypt("refresh"),token_expires_at=datetime.now(UTC)-timedelta(seconds=1)))
            session.commit()
        page=client.get("/own-sites/1/integrations/google_search_console/resources")
        form=BeautifulSoup(page.text,"html.parser").find("form",attrs={"action":"/own-sites/1/integrations/google_search_console/resources/refresh"})
        payload={item["name"]:item.get("value","") for item in form.find_all("input") if item.get("name")}
        response=client.post(form["action"],data=payload)
        assert response.status_code==200 and "sc-domain:example.test" in response.text
        with Session(app.state.engine) as session:
            connection=session.get(IntegrationConnection,1)
            assert box.decrypt(connection.access_token_encrypted)=="fresh"
            assert box.decrypt(connection.refresh_token_encrypted)=="rotated"
            assert not session.exec(select(IntegrationSource)).all()
            assert not session.exec(select(IntegrationSyncRun)).all()


def test_connect_is_protected_post_and_secrets_never_render(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);app=create_app(active)
    with TestClient(app) as client:
        client.post("/own-sites",data={"name":"Свой","url":"https://own.test"})
        assert client.get("/own-sites/1/integrations/google_search_console/connect").status_code==405
        page=client.get("/own-sites/1/integrations");assert "google-secret" not in page.text
        forbidden=client.post("/own-sites/1/integrations/google_search_console/connect",data={"action_token":"bad"},follow_redirects=False)
        assert forbidden.status_code==303


def _connected(tmp_path,monkeypatch,provider=GOOGLE):
    active=settings(tmp_path,monkeypatch);engine=build_engine(active.database_url);initialize_database(engine);box=SecretBox(active.data_dir)
    with Session(engine) as session:
        session.add(Site(id=1,name="Сайт",url="https://example.test",site_type=SITE_TYPE_OWNED));session.add(IntegrationConnection(id=1,site_id=1,provider=provider,status="connected",access_token_encrypted=box.encrypt("old-access"),refresh_token_encrypted=box.encrypt("old-refresh"),token_expires_at=datetime(2026,7,22,tzinfo=UTC),provider_user_id="42" if provider==YANDEX else None));session.commit()
    source=select_resource(engine,1,{"id":"sc-domain:example.test" if provider==GOOGLE else "host-id","label":"example.test"})
    return active,engine,box,source


def test_google_real_pagination_limit_and_partial(tmp_path,monkeypatch):
    import marketing_intelligence.integrations as module
    active,engine,box,source=_connected(tmp_path,monkeypatch);monkeypatch.setattr(module,"GOOGLE_PAGE_SIZE",2);monkeypatch.setattr(module,"MAX_ROWS",4)
    calls=[]
    def handler(request):
        if request.url.path.endswith("/token"):return httpx.Response(200,json={"access_token":"new-access","expires_in":3600})
        body=__import__("json").loads(request.content);calls.append(body);offset=body["startRow"]
        rows=[{"keys":["2026-07-19",f"https://example.test/{offset+i}"],"clicks":1.0,"impressions":2.0,"position":str(i+1)} for i in range(2)]
        return httpx.Response(200,json={"rows":rows})
    asyncio.run(execute_pending(engine,box,active,httpx.AsyncClient(transport=httpx.MockTransport(handler)),TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
    assert [(c["rowLimit"],c["startRow"]) for c in calls]==[(2,0),(2,2)]
    with Session(engine) as session:
        run=session.exec(select(IntegrationSyncRun).order_by(IntegrationSyncRun.id.desc())).first();assert run.status=="partial" and run.requested_start==date(2026,6,20) and run.requested_end==date(2026,7,20);assert len(session.exec(select(IntegrationPageMetric)).all())==4


def test_yandex_official_statistics_count_pagination_and_aggregation(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch,YANDEX);requests=[]
    def handler(request):
        if request.url.path.endswith("/token"):return httpx.Response(200,json={"access_token":"new","refresh_token":"rotated","expires_in":3600})
        body=__import__("json").loads(request.content);requests.append(body);offset=body["offset"]
        url=f"https://example.test/{offset}"
        item={"text_indicator":{"type":"URL","value":url},"popular_complementary_indicator":{"type":"QUERY","value":"q"},"statistics":[{"date":"2026-07-19","field":"CLICKS","value":1.0},{"date":"2026-07-19","field":"IMPRESSIONS","value":4.0},{"date":"2026-07-19","field":"POSITION","value":2.50},{"date":"2026-07-18","field":"IMPRESSIONS","value":0.0}]}
        return httpx.Response(200,json={"count":2,"text_indicator_to_statistics":[item]})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler));asyncio.run(execute_pending(engine,box,active,client,TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
    assert [r["offset"] for r in requests]==[0,1] and all(r["limit"]==500 for r in requests)
    assert all(r["device_type_indicator"]=="ALL" and r["search_location"]=="WEB_LOCATION" and r["text_indicator"]=="URL" for r in requests)
    with Session(engine) as session:
        rows=session.exec(select(IntegrationPageMetric).order_by(IntegrationPageMetric.normalized_url,IntegrationPageMetric.metric_date)).all();assert len(rows)==4;assert rows[0].impressions==0 and rows[0].clicks is None;connection=session.get(IntegrationConnection,1);assert box.decrypt(connection.refresh_token_encrypted)=="rotated"


def test_401_refreshes_once_preserves_google_refresh_and_retries_once(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch);calls={"api":0,"refresh":0}
    with Session(engine) as session:c=session.get(IntegrationConnection,1);c.token_expires_at=datetime(2027,1,1,tzinfo=UTC);session.add(c);session.commit()
    def handler(request):
        if request.url.path.endswith("/token"):calls["refresh"]+=1;return httpx.Response(200,json={"access_token":"fresh","expires_in":3600})
        calls["api"]+=1
        if calls["api"]==1:return httpx.Response(401,json={"error":"unauthorized"})
        return httpx.Response(200,json={"rows":[]})
    asyncio.run(execute_pending(engine,box,active,httpx.AsyncClient(transport=httpx.MockTransport(handler)),TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
    assert calls=={"api":2,"refresh":1}
    with Session(engine) as session:c=session.get(IntegrationConnection,1);assert box.decrypt(c.refresh_token_encrypted)=="old-refresh";assert session.exec(select(IntegrationSyncRun).order_by(IntegrationSyncRun.id.desc())).first().status=="completed"


def test_invalid_grant_requires_reauthorization_and_keeps_metrics(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:session.add(IntegrationPageMetric(source_id=source.id,provider=GOOGLE,metric_date=date(2026,7,1),normalized_url="https://example.test/old",clicks=1,impressions=2));session.commit()
    def handler(request):return httpx.Response(400,json={"error":"invalid_grant"})
    asyncio.run(execute_pending(engine,box,active,httpx.AsyncClient(transport=httpx.MockTransport(handler)),TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
    with Session(engine) as session:assert session.get(IntegrationConnection,1).status=="reauthorization_required";assert len(session.exec(select(IntegrationPageMetric)).all())==1


def test_resource_change_is_idempotent_and_rejects_stale_form(tmp_path,monkeypatch):
    active,engine,box,first=_connected(tmp_path,monkeypatch)
    second=select_resource(engine,1,{"id":"https://example.test/","label":"prefix"},expected_source_id=first.id)
    same=select_resource(engine,1,{"id":"https://example.test/","label":"prefix"},expected_source_id=second.id);assert same.id==second.id
    try:select_resource(engine,1,{"id":"sc-domain:example.test","label":"domain"},expected_source_id=first.id)
    except IntegrationError as exc:assert exc.code=="stale_resource"
    else:assert False
    with Session(engine) as session:assert len(session.exec(select(IntegrationSource)).all())==2;assert len([x for x in session.exec(select(IntegrationSource)).all() if x.active])==1


def test_new_oauth_account_deactivates_old_source_without_deleting_history(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:session.add(IntegrationPageMetric(source_id=source.id,provider=GOOGLE,metric_date=date(2026,7,1),normalized_url="https://example.test/old",clicks=1,impressions=2));session.commit()
    auth=start_oauth(engine,box,1,GOOGLE,active.google_oauth,another=True);state=parse_qs(urlsplit(auth).query)["state"][0]
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"access_token":"other-access","refresh_token":"other-refresh","expires_in":3600}))) as client:return await finish_oauth(engine,box,active,GOOGLE,state,"code",client)
    assert asyncio.run(run())==1
    with Session(engine) as session:
        assert not session.exec(select(IntegrationSource).where(IntegrationSource.active==True)).all();assert len(session.exec(select(IntegrationPageMetric)).all())==1;assert box.decrypt(session.get(IntegrationConnection,1).access_token_encrypted)=="other-access"


def test_untrusted_http_responses_are_bounded_and_sanitized():
    cases=[httpx.Response(429,json={"error":"quota","secret":"do-not-show"}),httpx.Response(503,json={"error":"down"}),httpx.Response(200,content=b"not-json",headers={"content-type":"application/json"}),httpx.Response(200,content=b"x"*(2*1024*1024+1),headers={"content-type":"application/json"})]
    async def run(response):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:response)) as client:
            try:await safe_json(client,"GET","https://fixed.example/api")
            except IntegrationError as exc:return str(exc),exc.code
        assert False
    results=[asyncio.run(run(response)) for response in cases]
    assert [code for _,code in results]==["retryable","retryable","invalid_json","invalid_response"]
    assert all("do-not-show" not in message for message,_ in results)


def test_second_page_error_rolls_back_complete_dataset(tmp_path,monkeypatch):
    import marketing_intelligence.integrations as module
    active,engine,box,source=_connected(tmp_path,monkeypatch);monkeypatch.setattr(module,"GOOGLE_PAGE_SIZE",1)
    with Session(engine) as session:session.add(IntegrationPageMetric(source_id=source.id,provider=GOOGLE,metric_date=date(2026,7,1),normalized_url="https://example.test/old",clicks=1,impressions=2));session.commit()
    calls=0
    def handler(request):
        nonlocal calls
        if request.url.path.endswith("/token"):return httpx.Response(200,json={"access_token":"fresh","expires_in":3600})
        calls+=1
        return httpx.Response(200,json={"rows":[{"keys":["2026-07-19","https://example.test/new" if calls==1 else "https://outside.test/bad"],"clicks":1,"impressions":2,"position":1}]})
    asyncio.run(execute_pending(engine,box,active,httpx.AsyncClient(transport=httpx.MockTransport(handler)),TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
    with Session(engine) as session:
        rows=session.exec(select(IntegrationPageMetric)).all();assert len(rows)==1 and rows[0].normalized_url.endswith("/old");assert session.exec(select(IntegrationSyncRun).order_by(IntegrationSyncRun.id.desc())).first().status=="failed"


def test_background_job_rechecks_owned_type(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:site=session.get(Site,1);site.site_type=SITE_TYPE_COMPETITOR;session.add(site);session.commit()
    asyncio.run(execute_pending(engine,box,active,httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(500,json={}))),TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
    with Session(engine) as session:run=session.exec(select(IntegrationSyncRun).order_by(IntegrationSyncRun.id.desc())).first();assert run.status=="cancelled" and not session.exec(select(IntegrationPageMetric)).all()


def test_late_401_after_disconnect_keeps_disconnected_and_finishes_run(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,1);connection.token_expires_at=datetime(2027,1,1,tzinfo=UTC);session.add(connection);session.commit()

    async def scenario():
        started=asyncio.Event();release=asyncio.Event()
        async def handler(request):
            if request.url.path.endswith("/searchAnalytics/query"):
                started.set();await release.wait();return httpx.Response(401,json={"error":"unauthorized"})
            return httpx.Response(400,json={"error":"invalid_grant"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task=asyncio.create_task(execute_pending(engine,box,active,client,TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
            await started.wait();disconnect(engine,1,GOOGLE);release.set();await task
    asyncio.run(scenario())

    with Session(engine) as session:
        connection=session.get(IntegrationConnection,1);run=session.exec(select(IntegrationSyncRun).order_by(IntegrationSyncRun.id.desc())).first()
        assert connection.status=="disconnected"
        assert connection.access_token_encrypted is None and connection.refresh_token_encrypted is None
        assert run.status in {"interrupted","cancelled"} and run.completed_at is not None


def test_site_delete_during_external_request_is_controlled_and_next_tick_runs(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,1);connection.token_expires_at=datetime(2027,1,1,tzinfo=UTC);session.add(connection);session.commit()

    async def scenario():
        started=asyncio.Event();release=asyncio.Event()
        async def handler(request):
            started.set();await release.wait();return httpx.Response(200,json={"rows":[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task=asyncio.create_task(execute_pending(engine,box,active,client,TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
            await started.wait();assert delete_site(engine,1);release.set();assert await task is True
            assert await execute_pending(engine,box,active,client,TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)) is False
    asyncio.run(scenario())
    with Session(engine) as session:
        assert session.get(Site,1) is None
        assert not session.exec(select(IntegrationConnection)).all()
        assert not session.exec(select(IntegrationSource)).all()
        assert not session.exec(select(IntegrationPageMetric)).all()
        assert not session.exec(select(IntegrationSyncRun)).all()


def test_new_oauth_account_interrupts_old_running_sync_without_mixing(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,1);connection.token_expires_at=datetime(2027,1,1,tzinfo=UTC);session.add(connection);session.commit()

    async def scenario():
        started=asyncio.Event();release=asyncio.Event()
        async def old_api(request):
            started.set();await release.wait();return httpx.Response(200,json={"rows":[{"keys":["2026-07-19","https://example.test/old-account"],"clicks":1,"impressions":2,"position":1}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(old_api)) as client:
            task=asyncio.create_task(execute_pending(engine,box,active,client,TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
            await started.wait()
            auth=start_oauth(engine,box,1,GOOGLE,active.google_oauth,another=True);state=parse_qs(urlsplit(auth).query)["state"][0]
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"access_token":"new-account","refresh_token":"new-refresh","expires_in":3600}))) as oauth_client:
                await finish_oauth(engine,box,active,GOOGLE,state,"code",oauth_client)
            release.set();await task
    asyncio.run(scenario())
    with Session(engine) as session:
        connection=session.get(IntegrationConnection,1);run=session.exec(select(IntegrationSyncRun).order_by(IntegrationSyncRun.id.desc())).first()
        assert connection.status=="connected" and box.decrypt(connection.access_token_encrypted)=="new-account"
        assert run.status=="interrupted" and not session.exec(select(IntegrationPageMetric)).all()
        assert not session.exec(select(IntegrationSource).where(IntegrationSource.active==True)).all()


def test_site_delete_during_token_refresh_does_not_restore_connection(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    started=asyncio.Event();release=asyncio.Event()
    async def handler(request):
        if request.url.path.endswith("/token"):
            started.set();await release.wait();return httpx.Response(200,json={"access_token":"late","refresh_token":"late-refresh","expires_in":3600})
        return httpx.Response(200,json={"rows":[]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task=asyncio.create_task(execute_pending(engine,box,active,client,TokenLocks(),now=datetime(2026,7,22,tzinfo=UTC)))
            await started.wait();assert delete_site(engine,1);release.set();assert await task is True
    asyncio.run(scenario())
    with Session(engine) as session:assert not session.exec(select(IntegrationConnection)).all() and not session.exec(select(IntegrationSyncRun)).all()


def test_stale_schedule_form_cannot_reenable_after_disconnect(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch);app=create_app(active)
    with TestClient(app) as client:
        page=client.get("/own-sites/1/integrations/google_search_console/schedule")
        form=BeautifulSoup(page.text,"html.parser").find("form")
        payload={item["name"]:item.get("value","") for item in form.find_all("input") if item.get("name")}
        payload.update(enabled="1",frequency="daily",local_weekday="0",local_time="09:00")
        disconnect(app.state.engine,1,GOOGLE)
        client.post("/own-sites/1/integrations/google_search_console/schedule",data=payload)
        with Session(app.state.engine) as session:
            schedule=session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==1)).one()
            assert schedule.enabled is False and schedule.next_run_at is None


def test_stale_resource_form_cannot_create_source_after_disconnect(tmp_path,monkeypatch):
    active=settings(tmp_path,monkeypatch);app=create_app(active,integration_transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"siteEntry":[{"siteUrl":"sc-domain:example.test","permissionLevel":"siteOwner"}]})))
    with TestClient(app) as client:
        client.post("/own-sites",data={"name":"Свой","url":"https://example.test"})
        box=app.state.integration_box
        with Session(app.state.engine) as session:
            session.add(IntegrationConnection(id=1,site_id=1,provider=GOOGLE,status="connected",access_token_encrypted=box.encrypt("access"),refresh_token_encrypted=box.encrypt("refresh"),token_expires_at=datetime(2027,1,1,tzinfo=UTC)))
            session.commit()
        page=client.get("/own-sites/1/integrations/google_search_console/resources")
        form=BeautifulSoup(page.text,"html.parser").find("form")
        payload={item["name"]:item.get("value","") for item in form.find_all("input") if item.get("name")};payload["resource_id"]="sc-domain:example.test"
        disconnect(app.state.engine,1,GOOGLE)
        client.post("/own-sites/1/integrations/google_search_console/resources",data=payload)
        with Session(app.state.engine) as session:assert not session.exec(select(IntegrationSource)).all()


def test_restart_same_database_and_key_preserves_and_decrypts_integration(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:
        session.add(IntegrationPageMetric(source_id=source.id,provider=GOOGLE,metric_date=date(2026,7,19),normalized_url="https://example.test/page",clicks=2,impressions=5));session.commit()
    engine.dispose()
    restarted_engine=build_engine(active.database_url);initialize_database(restarted_engine);restarted_box=SecretBox(active.data_dir)
    with Session(restarted_engine) as session:
        connection=session.get(IntegrationConnection,1)
        assert restarted_box.decrypt(connection.access_token_encrypted)=="old-access"
        assert restarted_box.decrypt(connection.refresh_token_encrypted)=="old-refresh"
        assert session.get(IntegrationSource,source.id).resource_id==source.resource_id
        assert session.exec(select(IntegrationPageMetric)).one().clicks==2


def test_delete_after_restart_preservation_removes_integration_separately(tmp_path,monkeypatch):
    active,engine,box,source=_connected(tmp_path,monkeypatch)
    with Session(engine) as session:
        session.add(IntegrationPageMetric(source_id=source.id,provider=GOOGLE,metric_date=date(2026,7,19),normalized_url="https://example.test/page",clicks=2,impressions=5));session.commit()
    engine.dispose();restarted_engine=build_engine(active.database_url);initialize_database(restarted_engine)
    with Session(restarted_engine) as session:assert session.exec(select(IntegrationPageMetric)).one().clicks==2
    assert delete_site(restarted_engine,1)
    with Session(restarted_engine) as session:
        assert not session.exec(select(IntegrationConnection)).all() and not session.exec(select(IntegrationPageMetric)).all()
