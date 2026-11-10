"""Microsoft Entra ID SSO: identity domain federated with Entra ID; Essbase still gets
identity-domain tokens, and each person's filters apply."""
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from essbase_mcp.config import Settings
from essbase_mcp.mock import server as mock
from essbase_mcp.web.app import Session, create_app

H = {"X-Requested-With": "essbase-mcp-assistant"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    s = Settings(mode="mock", identity_mode="per_user", sso_provider="entra", auto_signin=True,
                 context_dir=str(tmp_path / "c"), data_dir=str(tmp_path / "d"))
    yield create_app(Session(s))
    mock.IDP_POLICY = "local"


def follow(c: TestClient, url: str, max_hops: int = 10):
    """Follow redirects like a browser, returning (final response, list of paths visited)."""
    hops = []
    for _ in range(max_hops):
        u = urlparse(url)
        r = c.get(f"{u.path}?{u.query}" if u.query else u.path)
        hops.append(u.path)
        if r.status_code != 302:
            return r, hops
        loc = r.headers["location"]
        url = loc if loc.startswith(("/", "http")) else _join(u.path, loc)
    raise AssertionError(f"redirect loop: {hops}")


def _join(base_path: str, rel: str) -> str:
    from urllib.parse import urljoin
    return urljoin("http://testserver" + base_path, rel)


def sso(c: TestClient, upn: str | None):
    """Open /auth/login. If Microsoft shows the account picker, choose `upn`."""
    r, hops = follow(c, "/auth/login")
    picked = False
    if r.status_code == 200 and "Pick an account" in r.text:
        assert upn, "picker shown but no account to pick"
        req = r.text.split('name="request" value="')[1].split('"')[0]
        r = c.post("/mock/entra/signin", data={"upn": upn, "request": req})
        r, more = follow(c, _join("/mock/entra/signin", r.headers["location"]))
        hops += more
        picked = True
    return r, hops, picked


def client(app):
    return TestClient(app, headers=H, follow_redirects=False)


def test_first_sign_in_picks_microsoft_account_then_lands_in_app(app):
    c = client(app)
    me = c.get("/api/me").json()
    assert me["sso_provider"] == "entra" and me["auto_signin"] is True and not me["signed_in"]
    _, hops, picked = sso(c, "carol@contoso.com")
    assert picked and "/mock/entra/saml" in hops and "/auth/callback" in hops
    assert hops[-1] == "/" and c.get("/api/me").json()["name"] == "Carol Controller"


def test_already_signed_in_to_microsoft_means_no_pages_at_all(app):
    c = client(app)
    sso(c, "alice@contoso.com")
    c.post("/auth/logout")  # leave the app, stay signed in to Microsoft
    c.cookies.delete("emcp_session")
    _, hops, picked = sso(c, None)
    assert not picked  # straight through: app -> identity domain -> Entra -> app
    assert hops == ["/auth/login", "/mock/oauth2/v1/authorize", "/mock/entra/saml",
                    "/mock/oauth2/v1/federated", "/auth/callback", "/"]
    assert c.get("/api/me").json()["name"] == "Alice Analyst"


def test_filters_follow_the_microsoft_identity(app):
    carol, alice = client(app), client(app)
    sso(carol, "carol@contoso.com")
    carol.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    sso(alice, "alice@contoso.com")
    q = {"question": "Sales by market"}
    a = {r["member"]: r["values"][0] for r in alice.post("/api/ask", json=q).json()["result"]["rows"]}
    c = {r["member"]: r["values"][0] for r in carol.post("/api/ask", json=q).json()["result"]["rows"]}
    assert a["East"] == c["East"] and a["West"] is None and c["West"] is not None


def test_microsoft_user_without_identity_domain_account_is_refused(app):
    c = client(app)
    r, _, _ = sso(c, "dave@contoso.com")
    assert r.status_code == 403 and "provision" in r.text
    assert not c.get("/api/me").json()["signed_in"]


def test_sign_out_can_also_end_the_microsoft_session(app):
    c = client(app)
    sso(c, "bob@contoso.com")
    out = c.post("/auth/logout").json()
    assert out["redirect"] == "/mock/entra/signout?return=/"
    follow(c, out["redirect"])
    c.cookies.delete("emcp_session")
    _, _, picked = sso(c, "carol@contoso.com")
    assert picked  # Microsoft asks again, so someone else can sign in on this computer


def test_identity_domain_page_when_sso_off(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    s = Settings(mode="mock", identity_mode="per_user", context_dir=str(tmp_path / "c"),
                 data_dir=str(tmp_path / "d"))
    c = client(create_app(Session(s)))
    r, hops = follow(c, "/auth/login")
    assert r.status_code == 200 and "Mock identity domain" in r.text and "/mock/entra/saml" not in hops
    assert c.post("/auth/logout").json()["redirect"] is None


def test_admin_turns_sso_on(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    s = Settings(mode="mock", context_dir=str(tmp_path / "c"), data_dir=str(tmp_path / "d"))
    c = client(create_app(Session(s)))
    c.put("/api/admin/settings", json={"identity_mode": "per_user", "sso_provider": "entra",
                                       "auto_signin": True})
    v = c.get("/api/admin/settings").json()
    assert v["sso_provider"] == "entra" and v["auto_signin"] is True
    assert c.put("/api/admin/settings", json={"sso_provider": "okta"}).status_code == 422
    mock.IDP_POLICY = "local"
