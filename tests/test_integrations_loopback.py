from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import socket, threading, time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import httpx, uvicorn
from sqlmodel import Session, select

from marketing_intelligence.config import Settings
from marketing_intelligence.main import create_app
from marketing_intelligence.models import IntegrationConnection, IntegrationPageMetric, IntegrationSyncRun


def _port():
    with socket.socket() as sock:sock.bind(("127.0.0.1",0));return sock.getsockname()[1]


def _form(html,action):
    soup=BeautifulSoup(html,"html.parser");form=soup.find("form",attrs={"action":action});assert form
    return {item.get("name"):item.get("value","") for item in form.find_all("input") if item.get("name")}


def test_actual_uvicorn_tcp_oauth_sync_and_persistent_restart(tmp_path:Path,monkeypatch):
    for name in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv("MI_GOOGLE_CLIENT_ID","gid");monkeypatch.setenv("MI_GOOGLE_CLIENT_SECRET","gsecret");monkeypatch.setenv("MI_GOOGLE_REDIRECT_URI","http://127.0.0.1/oauth/google_search_console/callback")
    monkeypatch.setenv("MI_YANDEX_CLIENT_ID","yid");monkeypatch.setenv("MI_YANDEX_CLIENT_SECRET","ysecret");monkeypatch.setenv("MI_YANDEX_REDIRECT_URI","http://127.0.0.1/oauth/yandex_webmaster/callback")
    db=tmp_path/"data"/"loopback.db";active=Settings(data_dir=db.parent,logs_dir=tmp_path/"logs",database_url=f"sqlite:///{db.as_posix()}")
    def provider(request:httpx.Request):
        path=request.url.path
        if path.endswith("/token"):return httpx.Response(200,json={"access_token":"access-secret","refresh_token":"refresh-secret","expires_in":3600})
        if path.endswith("/webmasters/v3/sites"):return httpx.Response(200,json={"siteEntry":[{"siteUrl":"sc-domain:127.0.0.1","permissionLevel":"siteOwner"}]})
        if path.endswith("/v4/user"):return httpx.Response(200,json={"user_id":"u1"})
        if path.endswith("/hosts"):return httpx.Response(200,json={"hosts":[{"host_id":"h1","ascii_host_url":"http://127.0.0.1","verified":True}]})
        if path.endswith("/searchAnalytics/query"):return httpx.Response(200,json={"rows":[{"keys":["2026-07-19","http://127.0.0.1/a"],"clicks":2,"impressions":10,"position":"3.50"}]})
        if path.endswith("/query-analytics/list"):return httpx.Response(200,json={"rows":[{"date":"2026-07-19","url":"http://127.0.0.1/b","clicks":1,"shows":4,"position":"2.25"}]})
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
                    if len(session.exec(select(IntegrationSyncRun).where(IntegrationSyncRun.status=="completed")).all())==2:break
                time.sleep(.2)
            metrics=client.get("/own-sites/1/integrations/metrics?provider=google_search_console");assert "10" in metrics.text and "20.0%" in metrics.text
            assert "access-secret" not in db.read_bytes().decode("latin1") and "refresh-secret" not in db.read_bytes().decode("latin1")
    finally:
        server.should_exit=True;thread.join(timeout=10)
    app2=create_app(active,integration_transport=httpx.MockTransport(provider));server2=uvicorn.Server(uvicorn.Config(app2,host="127.0.0.1",port=_port(),log_level="error"));thread2=threading.Thread(target=server2.run,daemon=True);thread2.start();deadline=time.monotonic()+10
    while not server2.started and time.monotonic()<deadline:time.sleep(.02)
    assert server2.started
    try:
        with Session(app2.state.engine) as session:assert len(session.exec(select(IntegrationConnection)).all())==2 and len(session.exec(select(IntegrationPageMetric)).all())==2
    finally:server2.should_exit=True;thread2.join(timeout=10)
