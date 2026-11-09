import pytest
from fastapi.testclient import TestClient

from essbase_mcp.auth.providers import ClientCredentials
from essbase_mcp.context import builder
from essbase_mcp.mcp.client import MCPClient
from essbase_mcp.mock.server import CLIENT_ID, CLIENT_SECRET, app

BASE = "http://testserver"


@pytest.fixture
def http():
    return TestClient(app)


@pytest.fixture
def client(http):
    auth = ClientCredentials(f"{BASE}/oauth2/v1/token", CLIENT_ID, CLIENT_SECRET, "x", http)
    return MCPClient(f"{BASE}/essbase/rest/v1/ess-mcp", "viewer", auth, http)


@pytest.fixture
def ctx(client):
    c = builder.build(client, "Sample", "Basic")
    c["glossary"] = {"revenue": "Sales", "cola": "100-10", "plan": "Budget"}
    c["defaults"] = {"Scenario": "Actual"}
    return c
