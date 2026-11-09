"""Many users at once: one token, one session, no cross-talk, graceful 'busy'."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from essbase_mcp.auth.providers import ClientCredentials
from essbase_mcp.config import Settings
from essbase_mcp.mock import server as mock
from essbase_mcp.web.app import Session, create_app

H = {"X-Requested-With": "essbase-mcp-assistant"}
QUESTIONS = [
    ("Sample.Basic", "Sales by market", "Market"),
    ("Sample.Basic", "revenue for East by quarter", None),
    ("ASOSamp.Basic", "Units by geography", "Geography"),
    ("ASOSamp.Basic", "Units for Store 099", None),
    ("Sample.Basic", "top 3 products by sales with profit", None),
]


def test_token_refresh_is_single_flight():
    calls = []

    def handler(request):
        calls.append(1)
        time.sleep(0.05)  # slow identity domain
        return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_in": 3600})

    cc = ClientCredentials("http://idp/token", "id", "s", "x",
                           httpx.Client(transport=httpx.MockTransport(handler)))
    with ThreadPoolExecutor(50) as pool:
        tokens = set(pool.map(lambda _: cc.get_token(), range(50)))
    assert len(calls) == 1 and tokens == {"t1"}

    # a late 401 for an old token must not discard a newer one
    cc.invalidate("t1")
    assert cc.get_token() == "t2"
    cc.invalidate("t1")  # stale
    assert cc.get_token() == "t2" and len(calls) == 2


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    return Settings(mode="mock", context_dir=str(tmp_path / "c"), data_dir=str(tmp_path / "d"),
                    max_members_per_dimension=50, max_concurrent_calls=20)


def loaded(settings):
    web = TestClient(create_app(Session(settings)), headers=H)
    web.__enter__()  # run lifespan (thread pool sizing)
    for app in ("Sample", "ASOSamp"):
        assert web.post("/api/context/build", json={"application": app, "database": "Basic"}
                        ).status_code == 200
    return web


def test_100_concurrent_users(settings):
    web = loaded(settings)
    before = web.get("/api/metrics").json()

    def user(i):
        cube, q, _ = QUESTIONS[i % len(QUESTIONS)]
        r = web.post("/api/ask", json={"question": q, "cube": cube})
        return cube, q, r.status_code, r.json()

    with ThreadPoolExecutor(100) as pool:
        results = list(pool.map(user, range(200)))  # 200 questions from 100 users

    assert all(status == 200 for _, _, status, _ in results)
    assert all(body["ok"] and body["error"] is None for *_, body in results), [
        (q, b["plan"].get("clarification"), b["error"]) for _, q, _, b in results if not b["ok"]]
    # every answer belongs to the cube that user asked about (no cross-talk)
    assert all(body["cube"] == cube for cube, _, _, body in results)
    assert all(f"[{cube.split('.')[0]}].[Basic]" in body["steps"][-1]["arguments"]["query"]
               for cube, _, _, body in results)
    m = web.get("/api/metrics").json()
    assert m["token_fetches"] == 1                       # one sign-in for everyone
    assert m.get("initializations", 0) == before.get("initializations", 0)  # one MCP session
    assert m["peak_in_flight"] <= settings.max_concurrent_calls
    assert m.get("errors", 0) == 0
    # "Store 099" was searched once; concurrent askers waited for that one search
    searches = [s for _, q, _, b in results if "099" in q for s in b["steps"]
                if s["kind"] == "lookup" and s["status"] == "done"]
    assert len(searches) == 1


def test_server_drops_session_reinitializes_once(settings):
    web = loaded(settings)
    mock.SESSIONS.clear()  # Essbase restarted / session expired
    with ThreadPoolExecutor(30) as pool:
        codes = list(pool.map(lambda _: web.post("/api/ask", json={"question": "Sales by market"}
                                                 ).status_code, range(30)))
    assert codes == [200] * 30
    assert web.get("/api/metrics").json()["session_reinits"] == 1  # exactly one re-init
    assert len(mock.SESSIONS) == 1  # everyone moved to the same new session


def test_expired_token_mid_flight(settings):
    web = loaded(settings)
    mock._tokens.clear()  # identity domain revokes/expires every token
    with ThreadPoolExecutor(30) as pool:
        codes = list(pool.map(lambda _: web.post("/api/ask", json={"question": "Sales by market"}
                                                 ).status_code, range(30)))
    assert codes == [200] * 30
    assert web.get("/api/metrics").json()["token_fetches"] == 2  # one refresh, not 30


def test_overload_returns_busy_not_errors(settings, monkeypatch):
    settings.max_concurrent_calls, settings.queue_timeout_s = 2, 1
    web = loaded(settings)
    real = mock._run_mdx

    def slow(*a, **k):
        time.sleep(0.6)
        return real(*a, **k)

    monkeypatch.setattr(mock, "_run_mdx", slow)
    with ThreadPoolExecutor(12) as pool:
        codes = list(pool.map(lambda _: web.post("/api/ask", json={"question": "Sales by market"}
                                                 ).status_code, range(12)))
    assert set(codes) <= {200, 503} and 503 in codes and 200 in codes
    m = web.get("/api/metrics").json()
    assert m["peak_in_flight"] <= 2 and m["busy_rejections"] >= 1


def test_settings_change_while_users_are_asking(settings):
    web = loaded(settings)
    stop, codes = threading.Event(), []

    def asker():
        while not stop.is_set():
            codes.append(web.post("/api/ask", json={"question": "Sales by market"}).status_code)

    threads = [threading.Thread(target=asker) for _ in range(10)]
    for t in threads:
        t.start()
    for profile in ("analyst", "viewer", "analyst"):
        assert web.put("/api/admin/settings", json={"profile": profile}).json()["connection"]["ok"]
    stop.set()
    for t in threads:
        t.join()
    assert codes and set(codes) == {200}
