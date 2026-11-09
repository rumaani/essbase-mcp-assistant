from essbase_mcp.context import store
from essbase_mcp.mcp.client import MCPClient


def test_auth_and_tools(client):
    assert client.initialize()["serverInfo"]["name"] == "mock-essbase-mcp"
    names = {t["name"] for t in client.list_tools()}
    assert "execute_mdx_query" in names
    assert "run_calculation" not in names  # hidden from viewer profile


def test_bad_credentials_rejected(http):
    r = http.post("/oauth2/v1/token", data={"grant_type": "client_credentials",
                                            "client_id": "x", "client_secret": "y"})
    assert r.status_code == 401


def test_context_build(ctx):
    assert ctx["intents"]["data_query"] == "execute_mdx_query"
    assert ctx["intents"]["get_members"] == "get_members"
    assert {d["name"] for d in ctx["dimensions"]} >= {"Year", "Measures", "Market"}


def test_merge_preserves_curated(ctx):
    regenerated = dict(ctx, glossary={}, defaults={})
    merged = store.merge(regenerated, ctx)
    assert merged["glossary"]["revenue"] == "Sales"


def test_tool_call(client):
    client.initialize()
    res = client.call_tool("get_members", {"application": "Sample", "database": "Basic",
                                           "dimension": "Market", "parent": "East"})
    assert MCPClient.tool_payload(res)["members"][0]["name"] == "New York"
