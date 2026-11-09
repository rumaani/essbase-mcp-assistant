# Context file format (`contexts/<App>.<Db>.yaml`)

| Section | Source | Purpose |
|---|---|---|
| `source`, `cube`, `generated_at` | generated | Where and when it was built |
| `tools` | generated (`tools/list`) | Name, description, input schema |
| `intents` | suggested + **curated** | intent → tool name. Edit if the heuristic picks wrong |
| `dimensions` | generated | Dimension type, members, parents, aliases |
| `glossary` | **curated** | Business terms → member names (`revenue: Sales`) |
| `defaults` | **curated** | Implicit members per dimension (`Scenario: Actual`) |
| `examples` | **curated** | Sample questions and expected intents (regression set / LLM few-shot) |

Intents: `list_applications`, `list_databases`, `get_dimensions`, `get_members`,
`data_query`, `calculation`.

Tips:
- Put a team's vocabulary in `glossary`: "opex", "plan", "LE", product nicknames.
- Large outlines: limit members per dimension when building (roadmap) and rely on aliases + glossary.
