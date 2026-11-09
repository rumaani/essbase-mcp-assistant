import pytest
from fastapi.testclient import TestClient

from essbase_mcp.config import Settings
from essbase_mcp.web.app import Session, create_app

H = {"X-Requested-With": "essbase-mcp-assistant"}


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCP_ADMIN_PASSWORD", raising=False)
    s = Settings(mode="mock", context_dir=str(tmp_path / "c"), data_dir=str(tmp_path / "d"),
                 max_members_per_dimension=50)  # Stores (124 members) is only partly cached
    client = TestClient(create_app(Session(s)), headers=H)
    for app in ("Sample", "ASOSamp"):
        client.post("/api/context/build", json={"application": app, "database": "Basic"})
    return client


def ask(web, q, cube="ASOSamp.Basic", **kw):
    return web.post("/api/ask", json={"question": q, "cube": cube, **kw}).json()


def test_partial_outline_is_marked(web):
    ctx = web.get("/api/context", params={"cube": "ASOSamp.Basic"}).json()
    stores = next(d for d in ctx["dimensions"] if d["name"] == "Stores")
    assert stores["truncated"] and stores["total_members"] == 124 and len(stores["members"]) == 50


def test_member_lookup_then_query(web):
    r = ask(web, "Units for Store 117")
    kinds = [(s["kind"], s["status"]) for s in r["steps"]]
    assert kinds == [("lookup", "done"), ("query", "done")]
    assert r["steps"][0]["summary"] == "Stores:Store 117"
    assert "[Store 117]" in r["plan"]["arguments"]["query"]
    # the next user gets it from the shared index: no lookup step at all
    r2 = ask(web, "Price Paid for Store 117")
    assert [s["kind"] for s in r2["steps"]] == ["query"] and r2["ok"]
    assert r2["plan"]["entities"][-1]["via"] == "lookup"


def test_unknown_member_asks_instead_of_guessing(web):
    r = ask(web, "Units for Store 999")
    assert not r["ok"] and "Store 999" in r["plan"]["clarification"] and r["result"] is None
    # ambiguous partial ("Store 1" matches many) is not silently resolved either
    assert not ask(web, "Units for Store 1")["ok"]


def test_plan_only_shows_pending_lookup(web):
    r = ask(web, "Units for Store 088", execute=False)
    assert r["steps"][0]["kind"] == "lookup" and r["steps"][0]["status"] == "planned"


def test_top_n_single_step(web):
    r = ask(web, "top 2 markets by sales", cube="Sample.Basic")
    assert [s["kind"] for s in r["steps"]] == ["rank"]
    vals = [row["values"][0] for row in r["result"]["rows"]]
    assert len(vals) == 2 and vals == sorted(vals, reverse=True)


def test_top_n_then_follow_up_measures(web):
    r = ask(web, "top 2 markets by sales with profit", cube="Sample.Basic")
    assert [s["kind"] for s in r["steps"]] == ["rank", "query"]
    winners = r["steps"][0]["summary"].split("→ ")[1].split(", ")
    assert r["result"]["columns"] == ["Sales", "Profit"]
    assert [row["member"] for row in r["result"]["rows"]] == winners


def test_bottom_n_with_breakdown(web):
    r = ask(web, "bottom 2 products by sales by year", cube="Sample.Basic")
    assert r["ok"] and r["result"]["columns"] == ["Qtr1", "Qtr2", "Qtr3", "Qtr4"]
    rank = ask(web, "bottom 2 products by sales", cube="Sample.Basic")["result"]["rows"]
    assert [row["member"] for row in r["result"]["rows"]] == [row["member"] for row in rank]
