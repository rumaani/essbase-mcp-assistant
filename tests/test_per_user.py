"""Per-user sign-in: each person's own token reaches Essbase, so their filters apply."""
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from essbase_mcp.config import Settings
from essbase_mcp.mock import server as mock
from essbase_mcp.web.app import Session, create_app

H = {"X-Requested-With": "essbase-mcp-assistant"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    s = Settings(mode="mock", identity_mode="per_user", context_dir=str(tmp_path / "c"),
                 data_dir=str(tmp_path / "d"))
    return create_app(Session(s))


def sign_in(app, username: str) -> TestClient:
    """Drive the real browser round trip: app -> identity domain page -> callback -> cookie."""
    c = TestClient(app, headers=H, follow_redirects=False)
    r = c.get("/auth/login")
    assert r.status_code == 302
    auth = urlparse(r.headers["location"])
    assert auth.path == "/mock/oauth2/v1/authorize"
    q = {k: v[0] for k, v in parse_qs(auth.query).items()}
    assert q["code_challenge_method"] == "S256" and "offline_access" in q["scope"]
    page = c.get(r.headers["location"])
    assert page.status_code == 200 and "Alice Analyst" in page.text
    form = dict(re.findall(r'name="(\w+)" value="([^"]*)"', page.text))
    form = {k: v.replace("&amp;", "&") for k, v in form.items() if k != "username"}
    r = c.post("/mock/oauth2/v1/approve", data={**form, "username": username})
    assert r.status_code == 302
    cb = urlparse(r.headers["location"])
    r = c.get(f"{cb.path}?{cb.query}")
    assert r.status_code == 302 and "emcp_session" in r.cookies
    return c


def rows(resp):
    return {row["member"]: row["values"][0] for row in resp["result"]["rows"]}


def test_must_sign_in(app):
    anon = TestClient(app, headers=H)
    r = anon.post("/api/ask", json={"question": "Sales by market"})
    assert r.status_code == 401 and r.json()["signin"] is True
    assert anon.get("/api/context").status_code == 401
    me = anon.get("/api/me").json()
    assert me["identity_mode"] == "per_user" and me["signed_in"] is False


def test_each_user_sees_only_their_data(app):
    carol = sign_in(app, "carol")
    assert carol.get("/api/me").json()["name"] == "Carol Controller"
    assert carol.post("/api/context/build", json={"application": "Sample", "database": "Basic"}
                      ).status_code == 200
    alice, bob = sign_in(app, "alice"), sign_in(app, "bob")
    q = {"question": "Sales by market"}
    a, b, c = (rows(x.post("/api/ask", json=q).json()) for x in (alice, bob, carol))
    assert a["East"] is not None and a["West"] is None and a["South"] is None
    assert b["West"] is not None and b["East"] is None
    assert all(v is not None for v in c.values())
    # same question, same cube, same app - only the signed-in identity differs
    assert a["East"] == c["East"] and b["West"] == c["West"]


def test_pkce_and_single_use_code(app):
    c = TestClient(app, headers=H, follow_redirects=False)
    login = c.get("/auth/login").headers["location"]
    q = {k: v[0] for k, v in parse_qs(urlparse(login).query).items()}
    r = c.post("/mock/oauth2/v1/approve", data={
        "username": "alice", "redirect_uri": q["redirect_uri"], "state": q["state"],
        "scope": q["scope"], "code_challenge": q["code_challenge"]})
    cb = urlparse(r.headers["location"])
    params = parse_qs(cb.query)
    # someone who stole the code but not the verifier can't redeem it
    bad = c.post("/mock/oauth2/v1/token", data={
        "grant_type": "authorization_code", "code": params["code"][0],
        "redirect_uri": q["redirect_uri"], "code_verifier": "wrong",
        "client_id": mock.CLIENT_ID, "client_secret": mock.CLIENT_SECRET})
    assert bad.status_code == 400
    # a forged state is refused by the app
    assert c.get(f"{cb.path}?code=x&state=forged").status_code == 400


def test_refresh_and_expired_sign_in(app, monkeypatch):
    carol = sign_in(app, "carol")
    carol.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    monkeypatch.setattr(mock, "ACCESS_TTL", 1)  # tokens now die almost at once
    alice = sign_in(app, "alice")
    for _ in range(3):  # each call needs a refresh; refresh tokens rotate
        assert alice.post("/api/ask", json={"question": "Sales for East"}).json()["ok"]
    mock._refresh.clear()  # identity domain revokes refresh tokens (e.g. user disabled)
    mock._tokens.clear()
    r = alice.post("/api/ask", json={"question": "Sales for East"})
    assert r.status_code == 401 and r.json()["signin"]


def test_logout(app):
    alice = sign_in(app, "alice")
    assert alice.get("/api/me").json()["signed_in"]
    alice.post("/auth/logout")
    alice.cookies.clear()
    assert not alice.get("/api/me").json()["signed_in"]


def test_many_users_concurrently_never_mix_identities(app):
    carol = sign_in(app, "carol")
    carol.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    names = ["alice", "bob", "carol"] * 20  # 60 people signed in at once
    with ThreadPoolExecutor(30) as pool:
        clients = list(pool.map(lambda n: (n, sign_in(app, n)), names))

    def ask(pair):
        name, client = pair
        return name, rows(client.post("/api/ask", json={"question": "Sales by market"}).json())

    with ThreadPoolExecutor(60) as pool:
        results = list(pool.map(ask, clients * 3))  # 180 questions in parallel
    expected = {"alice": {"East"}, "bob": {"West"}, "carol": {"East", "West", "South", "Central"}}
    for name, r in results:
        assert {m for m, v in r.items() if v is not None} == expected[name], name
    m = carol.get("/api/metrics").json()
    assert m["signed_in_users"] == 61 and m["peak_in_flight"] <= 20 and m.get("errors", 0) == 0


def test_switch_modes_from_admin(app):
    c = TestClient(app, headers=H)
    assert c.post("/api/ask", json={"question": "x"}).status_code == 401
    assert c.put("/api/admin/settings", json={"identity_mode": "shared"}).json()["connection"]["ok"]
    assert c.get("/api/me").json()["identity_mode"] == "shared"
    r = c.post("/api/admin/test", json={"identity_mode": "per_user"}).json()
    assert r["ok"] and r["per_user"]


def test_mock_routes_hidden_outside_mock_mode(tmp_path):
    s = Settings(mode="remote", mcp_url="http://127.0.0.1:9/x", token_url="http://127.0.0.1:9/t",
                 context_dir=str(tmp_path / "c"), data_dir=str(tmp_path / "d"))
    c = TestClient(create_app(Session(s)))
    assert c.get("/mock/oauth2/v1/authorize?client_id=x&redirect_uri=y").status_code == 404
