# Essbase MCP Assistant

A reference implementation for asking questions of **Oracle Essbase 26ai** cubes in plain English
through Essbase's built-in **MCP server**. It does three things:

1. **Authenticates** with OAuth 2.1 against an OCI IAM identity domain. Secrets stay server-side and are encrypted at rest.
2. **Understands the question** using a per-cube **context file**: the outline, aliases, a business glossary, defaults and free-form notes. It needs no LLM; any OpenAI-compatible model can be added as an optional hint.
3. **Calls the right MCP tool** with arguments validated against the tool's schema, and shows you exactly what it did.

It runs fully without a live 26ai: a built-in mock server with two sample cubes is the default.

```
"revenue for coastal markets in plan"
        │  entity linking: names, aliases, glossary   revenue→Sales, coastal→{New York, California}, plan→Budget
        │  intent rules (+ optional LLM hint)          data_query
        │  tool from context.intents                    execute_mdx_query
        ▼  args fitted + validated against inputSchema
SELECT {[Sales]} ON COLUMNS, NON EMPTY {[New York],[California]} ON ROWS
FROM [Sample].[Basic] WHERE ([Budget])
```

![Ask tab](docs/img/ask.png)

## Try it in 2 minutes (no Essbase needed)
```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
emcp web                                             # open http://localhost:8000
```

| Tab | What it does |
|---|---|
| **Ask** | Question box with per-cube examples. Shows recognized members (and whether they matched by name, alias or glossary), intent, tool and MDX next to the result. CSV export. |
| **Catalog** | Lists applications and cubes from the server. **Build** saves a cube's metadata to `contexts/<App>.<Db>.yaml`; **Use** makes it active. Switch cubes from the header too. |
| **Context** | Curate the active cube: glossary (one term → one or more members), defaults, free-form notes, intent → tool overrides, example questions. Everything is validated against the outline. |
| **Admin** | Connection settings that persist across restarts: mock vs. Essbase 26ai, endpoint, profile, OAuth client, TLS, optional LLM. **Test** before you **Save**. |

![Catalog tab](docs/img/catalog.png)

## Connecting to a real Essbase 26ai
1. In your OCI IAM identity domain, create a **confidential application** with the client-credentials grant. Note the client ID and secret, and the token URL `https://<domain>.identity.oraclecloud.com/oauth2/v1/token`.
2. Open **Admin**, choose **Essbase 26ai**, and enter the MCP endpoint `https://<host>/essbase/rest/v1/ess-mcp`, the profile, and the OAuth details.
3. Click **Test connection**, then **Save settings**.
4. Open **Catalog** and **Build** the cubes you need. Then check **Context → Intent → tool**: discovery guesses from tool names and descriptions, so confirm it picked the right tools.

See [docs/ADMIN.md](docs/ADMIN.md) for how settings and secrets are stored, and how to harden a shared deployment.

## Why this design
- **No LLM gateway required.** Rules plus the context file handle common questions deterministically. An LLM can only *suggest* an intent; members and tools are always checked against the context file.
- **Tool names aren't hard-coded.** Tools are discovered via `tools/list` and mapped to intents in the context file, so a different catalog means a rebuild, not a code change.
- **Structured and free-form context.** The glossary and defaults are exact and validated. Notes capture business rules a mapping can't express (see [docs/CONTEXT_FILE.md](docs/CONTEXT_FILE.md)).
- **Server-side secrets.** The browser never sees client secrets or tokens. Saved secrets are encrypted, and state-changing calls need a CSRF header.

![Context tab](docs/img/context.png)

## Command line
```bash
emcp apps                                       # applications and cubes on the server
emcp context build --app ASOSamp --db Basic     # save metadata -> contexts/ASOSamp.Basic.yaml
emcp ask "Units by geography" --dry-run         # show the plan only
emcp ask "Units by geography"                   # plan + run
emcp --remote tools                             # force the configured server for one command
pytest                                          # 20 tests, all against the mock
```

## Layout
```
src/essbase_mcp/
  config.py, settings_store.py   settings (defaults < .env < Admin tab), encrypted secrets
  runtime.py                     mock vs. remote connection
  auth/       token providers (client credentials, static token) + PKCE helpers
  mcp/        JSON-RPC 2.0 / Streamable HTTP client (JSON + SSE, session id, 401 retry)
  context/    context builder, argument-name fitting, per-cube YAML store, validation
  planner/    entity linker, intent rules, MDX builder, plan validation
  llm/        optional OpenAI-compatible intent hint (gets glossary + notes)
  mock/       mock identity domain + Essbase MCP: Sample.Basic, ASOSamp.Basic
  web/        FastAPI backend-for-frontend + single-page UI (no Node build)
contexts/     context files (one per cube)
data/         saved settings + encryption key (git-ignored)
docs/         ARCHITECTURE, CONTEXT_FILE, ADMIN, ROADMAP
```

## Status
Tested against the mock only. The mock's tool names are placeholders until they're checked against a live 26ai catalog; see [docs/ROADMAP.md](docs/ROADMAP.md). Contributions and reports from real 26ai environments are welcome.

MIT licensed.
