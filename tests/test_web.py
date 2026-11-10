import json

import pytest
from fastapi.testclient import TestClient

from essbase_mcp.config import Settings
from essbase_mcp.web.app import Session, create_app

H = {"X-Requested-With": "essbase-mcp-assistant"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("EMCP_SECRET_KEY", raising=False)
    return Settings(mode="mock", context_dir=str(tmp_path / "contexts"),
                    data_dir=str(tmp_path / "data"))


@pytest.fixture
def web(settings):
    return TestClient(create_app(Session(settings)), headers=H)


def test_csrf_header_required(settings):
    raw = TestClient(create_app(Session(settings)))
    assert raw.post("/api/connect").status_code == 403


def test_catalog_build_and_per_request_cube(web):
    cat = web.get("/api/catalog").json()
    cubes = [d["cube"] for a in cat["applications"] for d in a["databases"]]
    assert cubes == ["ASOSamp.Basic", "Sample.Basic"]
    assert web.post("/api/ask", json={"question": "x"}).status_code == 409  # no context yet

    web.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    aso = web.post("/api/context/build", json={"application": "ASOSamp", "database": "Basic"}).json()
    assert {d["name"] for d in aso["dimensions"]} >= {"Geography", "Stores"}
    assert web.get("/api/status").json()["default_cube"] == "Sample.Basic"  # first one loaded
    assert len(web.get("/api/contexts").json()["contexts"]) == 2

    # each request names its own cube; nobody switches the cube for anyone else
    r = web.post("/api/ask", json={"question": "Units by geography", "cube": "ASOSamp.Basic"}).json()
    assert r["ok"] and [x["member"] for x in r["result"]["rows"]] == ["East", "West", "Central"]
    r = web.post("/api/ask", json={"question": "Sales by market"}).json()  # default cube
    assert r["cube"] == "Sample.Basic" and r["ok"]


def test_curated_context_edits(web):
    web.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    bad = web.patch("/api/context", json={"glossary": {"revenue": "Nope"}})
    assert bad.status_code == 422
    assert web.patch("/api/context", json={"defaults": {"Scenario": "Sales"}}).status_code == 422
    rev = web.get("/api/context").json()["revision"]
    ok = web.patch("/api/context", json={"revision": rev,
        "glossary": {"revenue": "Sales", "coastal": ["New York", "California"]},
        "defaults": {"Scenario": "Actual"},
        "notes": "Fiscal year starts in January. 'Plan' means Budget.",
        "intents": {"data_query": "execute_mdx_query"}})
    assert ok.status_code == 200 and ok.json()["revision"] == rev + 1
    # a second editor still holding the old revision is refused instead of overwriting
    stale = web.patch("/api/context", json={"revision": rev, "notes": "mine"})
    assert stale.status_code == 409
    # rebuild keeps curated sections
    ctx = web.post("/api/context/build", json={"application": "Sample", "database": "Basic"}).json()
    assert ctx["notes"].startswith("Fiscal") and ctx["glossary"]["coastal"] == ["New York", "California"]
    mdx = web.post("/api/ask", json={"question": "revenue for coastal", "execute": False}
                   ).json()["plan"]["arguments"]["query"]
    assert "{[New York], [California]} ON ROWS" in mdx and "[Actual]" in mdx


def test_context_without_revision_still_conflict_checked(web, settings):
    from essbase_mcp.context import store
    web.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    path = store.path_for(settings.context_dir, "Sample", "Basic")
    legacy = store.load(path)
    legacy.pop("revision")
    store.save(legacy, path)  # a file written before revisions existed
    rev = web.get("/api/context").json()["revision"]
    assert rev == 1
    assert web.patch("/api/context", json={"revision": rev, "notes": "A"}).status_code == 200
    assert web.patch("/api/context", json={"revision": rev, "notes": "B"}).status_code == 409


def test_admin_settings_persist_and_encrypt(web, settings):
    view = web.get("/api/admin/settings").json()
    assert view["mode"] == "mock" and "client_secret" not in view

    assert web.put("/api/admin/settings", json={"profile": "superuser"}).status_code == 422
    # test remote settings without saving: unreachable host fails cleanly
    t = web.post("/api/admin/test", json={"mode": "remote", "mcp_url": "http://127.0.0.1:9/x",
                                          "token_url": "http://127.0.0.1:9/t"}).json()
    assert t["ok"] is False and web.get("/api/status").json()["mode"] == "mock"

    r = web.put("/api/admin/settings", json={"client_id": "abc", "client_secret": "s3cret",
                                             "profile": "analyst"}).json()
    assert r["saved"] and r["settings"]["client_secret_set"] and r["connection"]["ok"]
    raw = json.loads((__import__("pathlib").Path(settings.data_dir) / "settings.json").read_text())
    assert raw["client_secret"].startswith("enc:") and "s3cret" not in json.dumps(raw)

    from essbase_mcp import settings_store
    reloaded = settings_store.apply_saved(Settings(data_dir=settings.data_dir))
    assert reloaded.client_secret == "s3cret" and reloaded.profile == "analyst"

    # blank secret keeps the stored one; clear_ removes it
    web.put("/api/admin/settings", json={"client_secret": ""})
    assert web.get("/api/admin/settings").json()["client_secret_set"]
    web.put("/api/admin/settings", json={"clear_client_secret": True})
    assert not web.get("/api/admin/settings").json()["client_secret_set"]


def test_admin_password(settings, monkeypatch):
    monkeypatch.setenv("EMCP_ADMIN_PASSWORD", "pw")
    web = TestClient(create_app(Session(settings)), headers=H)
    assert web.get("/api/admin/settings").status_code == 401
    assert web.get("/api/admin/settings", headers={"X-Admin-Password": "pw"}).status_code == 200


def test_friendly_error_for_bad_credentials(web):
    r = web.post("/api/admin/test", json={"mode": "remote",
                                          "mcp_url": "http://mock.local/essbase/rest/v1/ess-mcp",
                                          "token_url": "http://mock.local/oauth2/v1/token"}).json()
    assert r["ok"] is False and "mock.local" in r["error"]
