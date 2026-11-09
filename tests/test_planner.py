from essbase_mcp.mcp.client import MCPClient
from essbase_mcp.planner.planner import Planner


def test_data_query_with_glossary_and_group_by(ctx):
    p = Planner(ctx).plan("What was revenue for Cola in East by quarter?")
    assert p.ok and p.tool == "execute_mdx_query"
    mdx = p.arguments["query"]
    assert "[Sales]" in mdx and "[100-10]" in mdx and "[East]" in mdx
    assert "[Actual]" in mdx  # default scenario applied


def test_group_by_dimension(ctx):
    mdx = Planner(ctx).plan("profit by market for Qtr1").arguments["query"]
    assert "[Market].Children ON ROWS" in mdx and "[Qtr1]" in mdx


def test_alias_and_multi_members(ctx):
    mdx = Planner(ctx).plan("Sales for January and February in New York").arguments["query"]
    assert "{[Jan], [Feb]} ON ROWS" in mdx


def test_members_intent(ctx):
    p = Planner(ctx).plan("show the children of East")
    assert p.tool == "get_members"
    assert p.arguments == {"application": "Sample", "database": "Basic",
                           "dimension": "Market", "parent": "East"}


def test_unknown_asks_for_clarification(ctx):
    p = Planner(ctx).plan("hello there")
    assert not p.ok and p.clarification


def test_calc_not_available_to_viewer(ctx):
    p = Planner(ctx).plan("run the calc script")
    assert not p.ok  # calculation tool not in viewer catalog


def test_end_to_end(ctx, client):
    p = Planner(ctx).plan("Sales and COGS for West in Budget")
    client.initialize()
    data = MCPClient.tool_payload(client.call_tool(p.tool, p.arguments))
    assert data["columns"] == ["Sales", "COGS"]


def test_group_by_glossary_term_and_member(ctx):
    ctx["glossary"]["quarter"] = "Year"
    p = Planner(ctx)
    assert "[Year].Children ON ROWS" in p.plan("Sales for East by quarter").arguments["query"]
    assert "[East].Children ON ROWS" in p.plan("Sales by East").arguments["query"]
