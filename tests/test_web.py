import pytest
from fastapi.testclient import TestClient

from essbase_mcp.config import Settings
from essbase_mcp.web.app import create_app, demo_session


@pytest.fixture
def web(tmp_path):
    session = demo_session(Settings(context_file=str(tmp_path / "ctx.yaml")))
    return TestClient(create_app(session))


def test_ui_flow(web):
    assert "Essbase MCP Assistant" in web.get("/").text
    assert web.get("/api/status").json()["demo"] is True
    assert web.post("/api/ask", json={"question": "x"}).status_code == 409  # no context yet

    assert web.post("/api/connect").json()["ok"]
    ctx = web.post("/api/context/build", json={"application": "Sample", "database": "Basic"}).json()
    assert ctx["intents"]["data_query"] == "execute_mdx_query"

    assert web.put("/api/context/glossary", json={"glossary": {"revenue": "Nope"}}).status_code == 422
    assert web.put("/api/context/glossary", json={"glossary": {"revenue": "Sales"}}).status_code == 200

    r = web.post("/api/ask", json={"question": "revenue by market for Qtr1"}).json()
    assert r["ok"] and r["error"] is None
    assert [row["member"] for row in r["result"]["rows"]] == ["East", "West", "South", "Central"]

    plan_only = web.post("/api/ask", json={"question": "Sales for East", "execute": False}).json()
    assert plan_only["ok"] and plan_only["result"] is None


def test_rebuild_keeps_glossary(web):
    web.post("/api/context/build", json={"application": "Sample", "database": "Basic"})
    web.put("/api/context/glossary", json={"glossary": {"plan": "Budget"}})
    ctx = web.post("/api/context/build", json={"application": "Sample", "database": "Basic"}).json()
    assert ctx["glossary"] == {"plan": "Budget"}
