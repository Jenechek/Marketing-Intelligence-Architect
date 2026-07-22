from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import socket, threading, time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import httpx, uvicorn
from sqlmodel import Session, select

from marketing_intelligence.config import OAuthProviderConfig, Settings
from marketing_intelligence.main import create_app
from marketing_intelligence.models import IntegrationConnection, IntegrationPageMetric, IntegrationSchedule, IntegrationSource, IntegrationSyncRun


def _port():
    with socket.socket() as sock:sock.bind(("127.0.0.1",0));return sock.getsockname()[1]


def _form(html,action):
    soup=BeautifulSoup(html,"html.parser");form=soup.find("form",attrs={"action":action});assert form
    return {item.get("name"):item.get("value","") for item in form.find_all("input") if item.get("name")}


def test_actual_uvicorn_tcp_without_oauth_credentials(tmp_path:Path,monkeypatch):
    for name in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):monkeypatch.delenv(name,raising=False)
    db=tmp_path/"no-creds.db";active=Settings(data_dir=tmp_path/"data",logs_dir=tmp_path/"logs",database_url=f"sqlite:///{db.as_posix()}",google_oauth=OAuthProviderConfig(error="OAuth-клиент не настроен."),yandex_oauth=OAuthProviderConfig(error="OAuth-клиент не настроен."))
    app=create_app(active);port=_port();server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error"));thread=threading.Thread(target=server.run,daemon=True);thread.start();deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.02)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}",trust_env=False,follow_redirects=True) as client:
            client.post("/own-sites",data={"name":"Без OAuth","url":"https://example.test"});screen=client.get("/own-sites/1/integrations");assert screen.status_code==200 and screen.text.count("Не настроено")>=2
    finally:server.should_exit=True;thread.join(timeout=10)


def test_actual_uvicorn_tcp_oauth_sync_and_persistent_restart(tmp_path:Path,monkeypatch):
    for name in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv("MI_GOOGLE_CLIENT_ID","gid");monkeypatch.setenv("MI_GOOGLE_CLIENT_SECRET","gsecret");monkeypatch.setenv("MI_GOOGLE_REDIRECT_URI","http://127.0.0.1/oauth/google_search_console/callback")
    monkeypatch.setenv("MI_YANDEX_CLIENT_ID","yid");monkeypatch.setenv("MI_YANDEX_CLIENT_SECRET","ysecret");monkeypatch.setenv("MI_YANDEX_REDIRECT_URI","http://127.0.0.1/oauth/yandex_webmaster/callback")
    db=tmp_path/"data"/"loopback.db";active=Settings(data_dir=db.parent,logs_dir=tmp_path/"logs",database_url=f"sqlite:///{db.as_posix()}")
    provider_state={"invalid_refresh":False,"refreshes":0}
    def provider(request:httpx.Request):
        path=request.url.path
        if path.endswith("/token"):
            form=parse_qs(request.content.decode())
            if form.get("grant_type")==["refresh_token"]:
                provider_state["refreshes"]+=1
                if provider_state["invalid_refresh"]:return httpx.Response(400,json={"error":"invalid_grant","error_description":"secret must not leak"})
                return httpx.Response(200,json={"access_token":"rotated-access","refresh_token":"rotated-refresh","expires_in":3600})
            return httpx.Response(200,json={"access_token":"access-secret","refresh_token":"refresh-secret","expires_in":3600})
        if path.endswith("/webmasters/v3/sites"):return httpx.Response(200,json={"siteEntry":[{"siteUrl":"http://127.0.0.1/","permissionLevel":"siteOwner"},{"siteUrl":"sc-domain:127.0.0.1","permissionLevel":"siteFullUser"},{"siteUrl":"http://127.0.0.1/<script>alert(1)</script>","permissionLevel":"siteRestrictedUser"}]})
        if path.endswith("/v4/user"):return httpx.Response(200,json={"user_id":42})
        if path.endswith("/hosts"):return httpx.Response(200,json={"hosts":[{"host_id":"alias","ascii_host_url":"http://alias.test/","verified":False,"main_mirror":{"host_id":"h1","ascii_host_url":"http://127.0.0.1/","unicode_host_url":"http://127.0.0.1/","verified":True}}]})
        if path.endswith("/searchAnalytics/query"):return httpx.Response(200,json={"rows":[{"keys":["2026-07-19","http://127.0.0.1/a"],"clicks":2,"impressions":10,"position":"3.50"}]})
        if path.endswith("/query-analytics/list"):return httpx.Response(200,json={"count":1,"text_indicator_to_statistics":[{"text_indicator":{"type":"URL","value":"http://127.0.0.1/b"},"popular_complementary_indicator":{"type":"QUERY","value":"test"},"statistics":[{"date":"2026-07-19","field":"CLICKS","value":1.0},{"date":"2026-07-19","field":"IMPRESSIONS","value":4.0},{"date":"2026-07-19","field":"POSITION","value":2.25}]}]})
        return httpx.Response(404,json={"error":"unknown"})
    app=create_app(active,integration_transport=httpx.MockTransport(provider));port=_port();server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error"));thread=threading.Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.02)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}",trust_env=False,follow_redirects=True) as client:
            client.post("/own-sites",data={"name":"Loopback","url":"http://127.0.0.1"});client.post("/competitors",data={"name":"Конкурент","url":"http://other.test"})
            page=client.get("/own-sites/1/integrations");before=sha256(db.read_bytes()).hexdigest();assert client.get("/own-sites/2/integrations").status_code==404;assert sha256(db.read_bytes()).hexdigest()==before
            for provider_name in ("google_search_console","yandex_webmaster"):
                action=f"/own-sites/1/integrations/{provider_name}/connect";response=client.post(action,data=_form(page.text,action),follow_redirects=False);state=parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
                callback=client.get(f"/oauth/{provider_name}/callback",params={"state":state,"code":"one-time-code"});assert "code=" not in str(callback.url) and "state=" not in str(callback.url)
                page=client.get("/own-sites/1/integrations")
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                with Session(app.state.engine) as session:
                    if len(session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.status=="completed")).all())!=2:
                        time.sleep(.2);continue
                    deadline=0
                time.sleep(.2)
                metrics=client.get("/own-sites/1/integrations/metrics?provider=google_search_console");assert "10" in metrics.text and "20.0%" in metrics.text
                assert "access-secret" not in db.read_bytes().decode("latin1") and "refresh-secret" not in db.read_bytes().decode("latin1")

                # Ручная смена после автоподбора повторно проверяет официальный список.
                resource_page=client.get("/own-sites/1/integrations/google_search_console/resources");assert "&lt;script&gt;" in resource_page.text and "<script>alert(1)</script>" not in resource_page.text
                soup=BeautifulSoup(resource_page.text,"html.parser");form=soup.find("form");payload={item.get("name"):item.get("value","") for item in form.find_all("input") if item.get("name")};payload["resource_id"]="sc-domain:127.0.0.1"
                changed=client.post("/own-sites/1/integrations/google_search_console/resources",data=payload);assert "Ресурс изменён" in changed.text
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    with Session(app.state.engine) as session:
                        if len(session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.status=="completed")).all())>=3:break
                    time.sleep(.2)

                # Два повторных ручных POST создают только один активный запуск.
                page=client.get("/own-sites/1/integrations");sync_action="/own-sites/1/integrations/google_search_console/sync";sync_form=_form(page.text,sync_action)
                client.post(sync_action,data=sync_form);client.post(sync_action,data=sync_form)
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    with Session(app.state.engine) as session:
                        active=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==1,IntegrationSyncRun.status.in_(("pending","running")))).all()
                        done=session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==1,IntegrationSyncRun.status=="completed")).all()
                        if not active and len(done)>=3:break
                    time.sleep(.2)

                # Плановый запуск использует сохранённое правило и один catch-up.
                schedule_page=client.get("/own-sites/1/integrations/google_search_console/schedule");soup=BeautifulSoup(schedule_page.text,"html.parser");form=soup.find("form");schedule_form={item.get("name"):item.get("value","") for item in form.find_all("input") if item.get("name")};schedule_form.update(enabled="1",frequency="daily",local_weekday="0",local_time="09:00")
                client.post("/own-sites/1/integrations/google_search_console/schedule",data=schedule_form)
                with Session(app.state.engine) as session:schedule=session.exec(select(IntegrationSchedule).where(IntegrationSchedule.connection_id==1)).one();schedule.next_run_at=datetime.now(UTC)-__import__("datetime").timedelta(seconds=1);session.add(schedule);session.commit()
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    with Session(app.state.engine) as session:
                        if session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.trigger=="scheduled",IntegrationSyncRun.status=="completed")).first():break
                    time.sleep(.2)

                # Истекающий токен обновляется, invalid_grant оставляет историю и требует подключения.
                with Session(app.state.engine) as session:c=session.get(IntegrationConnection,1);c.token_expires_at=datetime.now(UTC)-__import__("datetime").timedelta(seconds=1);session.add(c);session.commit()
                page=client.get("/own-sites/1/integrations");client.post(sync_action,data=_form(page.text,sync_action));deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    with Session(app.state.engine) as session:
                        if provider_state["refreshes"] and not session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id==1,IntegrationSyncRun.status.in_(("pending","running")))).first():break
                    time.sleep(.2)
                provider_state["invalid_refresh"]=True
                with Session(app.state.engine) as session:c=session.get(IntegrationConnection,1);c.token_expires_at=datetime.now(UTC)-__import__("datetime").timedelta(seconds=1);session.add(c);session.commit()
                page=client.get("/own-sites/1/integrations");client.post(sync_action,data=_form(page.text,sync_action));deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    with Session(app.state.engine) as session:
                        if session.get(IntegrationConnection,1).status=="reauthorization_required":break
                    time.sleep(.2)
                assert "secret must not leak" not in client.get("/own-sites/1/integrations").text

                # Все обычные GET, включая внешнее чтение ресурсов и подтверждения, не меняют SQLite.
                provider_state["invalid_refresh"]=False
                before_gets=sha256(db.read_bytes()).hexdigest()
                for url in ("/own-sites/1/integrations","/own-sites/1/integrations/metrics?provider=google_search_console","/own-sites/1/integrations/google_search_console/schedule","/own-sites/1/integrations/google_search_console/resources","/own-sites/1/integrations/google_search_console/disconnect","/sites/1/transfer","/sites/1/delete"):
                    assert client.get(url).status_code==200
                assert sha256(db.read_bytes()).hexdigest()==before_gets

                # Отключение сохраняет показатели; перенос блокируется; удаление очищает всё.
                disconnect_page=client.get("/own-sites/1/integrations/google_search_console/disconnect");disconnect_form=_form(disconnect_page.text,"/own-sites/1/integrations/google_search_console/disconnect") if False else {i.get("name"):i.get("value","") for i in BeautifulSoup(disconnect_page.text,"html.parser").find("form").find_all("input")}
                with Session(app.state.engine) as session:before_metrics=len(session.exec(select(IntegrationPageMetric)).all())
                client.post("/own-sites/1/integrations/google_search_console/disconnect",data=disconnect_form)
                with Session(app.state.engine) as session:assert len(session.exec(select(IntegrationPageMetric)).all())==before_metrics
                transfer=client.get("/sites/1/transfer");transfer_form={i.get("name"):i.get("value","") for i in BeautifulSoup(transfer.text,"html.parser").find("form").find_all("input")};assert client.post("/sites/1/transfer",data=transfer_form).status_code==409
                delete_page=client.get("/sites/1/delete");delete_form={i.get("name"):i.get("value","") for i in BeautifulSoup(delete_page.text,"html.parser").find("form").find_all("input")};client.post("/sites/1/delete",data=delete_form)
                with Session(app.state.engine) as session:assert session.get(IntegrationConnection,1) is None and not session.exec(select(IntegrationPageMetric)).all() and not session.exec(select(IntegrationSource)).all()
    finally:
        server.should_exit=True;thread.join(timeout=10)
    app2=create_app(active,integration_transport=httpx.MockTransport(provider));server2=uvicorn.Server(uvicorn.Config(app2,host="127.0.0.1",port=_port(),log_level="error"));thread2=threading.Thread(target=server2.run,daemon=True);thread2.start();deadline=time.monotonic()+10
    while not server2.started and time.monotonic()<deadline:time.sleep(.02)
    assert server2.started
    try:
        with Session(app2.state.engine) as session:assert not session.exec(select(IntegrationConnection)).all() and not session.exec(select(IntegrationPageMetric)).all()
    finally:server2.should_exit=True;thread2.join(timeout=10)
