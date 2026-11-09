# Essbase MCP Assistant

Ask questions of an **Oracle Essbase 26ai** cube in plain English. The assistant authenticates
with OAuth 2.1 and turns each question into a validated call to the right **Essbase MCP** tool.
It's grounded in a curated **context file** and needs **no LLM**. You can plug in any
OpenAI-compatible model (Ollama, LM Studio, OpenAI) as an optional hint.

```
"Revenue and cost of sales for Colas by market in Qtr1"
        │  entity linking (names, aliases, glossary)  ─ revenue→Sales, Colas→100, Qtr1
        │  intent rules (+ optional LLM hint)          ─ data_query
        │  tool from context.intents                    ─ execute_mdx_query
        ▼  args fitted + validated against inputSchema
SELECT {[Sales],[COGS]} ON COLUMNS, NON EMPTY [Market].Children ON ROWS
FROM [Sample].[Basic] WHERE ([100],[Qtr1],[Actual])
```

## Why this design
- **Works without an LLM gateway.** Deterministic rules plus the context file handle common questions. An LLM can only *suggest* an intent; it can't invent members or tools.
- **Doesn't hard-code Oracle's tool names.** Tools are discovered via `tools/list` and mapped to intents in the context file, so a catalog change means rebuilding the context, not editing code.
- **Can be developed without a live 26ai server.** A bundled mock identity domain and Essbase MCP server (Sample.Basic) run everything locally.

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

python -m essbase_mcp.mock.server &               # mock IDCS + MCP on :9100
emcp context build --app Sample --db Basic        # -> contexts/Sample.Basic.yaml
emcp ask "Sales for East by quarter" --dry-run    # see the plan
emcp ask "Sales for East by quarter"              # run it
pytest
```

## Pointing at a real Essbase 26ai
1. Create a confidential app in your OCI IAM identity domain (client credentials, `client_secret_post`).
2. Set `ESSBASE_MCP_URL=https://<host>/essbase/rest/v1/ess-mcp`, `OAUTH_TOKEN_URL`, client ID and secret, and `ESSBASE_PROFILE`.
3. `emcp tools`. Check the intent mapping that `context build` suggests, and fix it in the YAML if needed.

## Layout
```
src/essbase_mcp/
  auth/       token providers (client credentials, static token) + PKCE helpers
  mcp/        JSON-RPC 2.0 / Streamable HTTP client (JSON + SSE, session id, 401 retry)
  context/    context builder, argument-name fitting, YAML store with curated-section merge
  planner/    entity linker, intent rules, MDX builder, plan validation
  llm/        optional OpenAI-compatible intent hint
  mock/       mock identity domain + Essbase MCP (placeholder tool catalog)
contexts/     generated + curated context files
docs/         ARCHITECTURE.md, CONTEXT_FILE.md, ROADMAP.md
```

## Status
v0.1 is tested against the mock only. The mock's tool names are placeholders until they're checked against a live 26ai catalog. See [docs/ROADMAP.md](docs/ROADMAP.md).

MIT licensed.
