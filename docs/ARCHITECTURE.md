# Architecture

```
user question
   │
   ▼
Planner ── MemberIndex (context.dimensions + glossary) ── entities, group-by
   │   └─ intent rules ── optional LLM hint (OpenAI-compatible, JSON only)
   │
   ├─ context.intents[intent] → tool name
   ├─ args.fit(tool.input_schema)  (application/appName/... synonyms)
   └─ validate required args → Plan (or clarification question)
   │
   ▼
MCPClient ── TokenProvider (OAuth 2.1 client credentials / static token)
   │  JSON-RPC 2.0 over Streamable HTTP, ?profile=viewer|analyst|admin
   ▼
Essbase 26ai MCP (or mock)
```

## Safety rails
- Tools come from `tools/list` for the configured profile, so the server's own profile gating holds. A viewer never sees admin tools.
- Calculations need `--yes`.
- Unknown questions return a clarification instead of a guess.
- The LLM output is a hint and is re-validated against the context file.

## Context lifecycle
`emcp context build` refreshes `tools`, `intents` (suggested), and `dimensions`. It preserves the
curated sections (`glossary`, `intents` overrides, `defaults`, `examples`). Rebuild whenever
the outline or the server's catalog changes.
