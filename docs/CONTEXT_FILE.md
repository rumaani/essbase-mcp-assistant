# Context file format (`contexts/<App>.<Db>.yaml`)

One file per cube. Load or refresh it from **Cubes** (or `emcp context build`). Curate it from
**Context**, where the curated sections appear as steps: Vocabulary = `glossary`, Assumptions = `defaults`,
Business rules = `notes`, Example questions = `examples`, and Advanced = `intents`. A refresh replaces the generated sections and keeps the curated ones.

| Section | Source | Purpose |
|---|---|---|
| `source`, `cube`, `generated_at` | generated | Where and when it was built |
| `tools` | generated (`tools/list`) | Name, description, input schema |
| `dimensions` | generated | Dimension type, members, parents, aliases |
| `intents` | suggested + **curated** | intent → tool name. Overrides survive a refresh while the tool still exists |
| `glossary` | **curated** | Business term → member, or a list of members |
| `defaults` | **curated** | Member used when a question doesn't name that dimension (`Scenario: Actual`) |
| `notes` | **curated** | Free-form business rules and conventions |
| `examples` | **curated** | Sample questions, shown as shortcuts on Ask (and a future regression set) |

```yaml
glossary:
  revenue: Sales                     # 1 -> 1
  coastal: [New York, California]    # 1 -> many: becomes a set on rows
  quarter: Year                      # "by quarter" -> Year's children
defaults:
  Scenario: Actual
notes: |
  - 'Plan' and 'forecast' both mean Budget.
  - Margin % = Margin / Sales.
```

## Structured mappings or free-form notes?
Use both. They do different jobs.

| | Glossary / defaults | Notes |
|---|---|---|
| Used by | Rules engine (always) and LLM | LLM (when configured) and people reading the context |
| Behavior | Exact, the same every time, validated against the outline | Interpreted, so it can express rules and nuance |
| Good for | Synonyms, nicknames, groupings, implicit members | Fiscal calendar, "use X unless Y", how to compute ratios, data caveats |
| Risk | Can't express conditions | Unchecked; a model may misapply it |

Rule of thumb: if it can be a mapping, make it a mapping. Put in notes what needs a sentence.

Intents: `list_applications`, `list_databases`, `get_dimensions`, `get_members`,
`data_query`, `calculation`.
