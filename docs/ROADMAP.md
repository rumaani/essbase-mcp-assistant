# Roadmap

- [ ] Verify against a live Essbase 26ai: real tool names/schemas, intent heuristics, response shapes
- [ ] Large-outline support: level/generation filters when loading
- [x] Multi-step plans: member lookup for partially loaded outlines, top/bottom N
- [x] Concurrency: shared token/session with single-flight recovery, call limiter, per-request cube, revisioned contexts, load test
- [ ] Richer MDX: time ranges ("Jan to Mar"), top-N, variance (Actual vs Budget), crossjoins
- [ ] Evaluate `examples` as a regression suite (`emcp eval`)
- [x] Web UI: ask / catalog / context / admin
- [x] Multiple cubes: list applications from the server, one context file per cube
- [x] Persistent, encrypted connection settings (Admin tab)
- [x] Free-form notes + one-to-many glossary terms
- [x] Per-user sign-in (OCI IAM auth code + PKCE): each person's Essbase filters apply
- [ ] Shared session store (Redis) for several app processes
- [ ] Entra ID federation guide (via the identity domain)
- [ ] Use notes in the rules engine (e.g. fiscal calendar) without an LLM
- [ ] Expose the assistant itself as an MCP server (so any MCP client can use it)
