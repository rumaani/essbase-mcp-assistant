# Multi-step questions and many concurrent users

## Multi-step questions
A question becomes a list of steps. Each step is one MCP tool call, and all steps show
under **Ask → Show details** with timing and MDX.

| Situation | Steps |
|---|---|
| Everything is in the context file | 1. get the numbers |
| A name isn't in the cached outline (e.g. *Store 117* in a large dimension) | 1. find it (`search_members`) → 2. get the numbers |
| *top / bottom N* | 1. rank the members by the measure → 2. get the requested measures or breakdown for the winners (skipped if the ranking already answers it) |

The POV (point of view) is worked out before calling Essbase. Mentioned members go where
the question puts them, *Assumptions* fill the slice, and other dimensions use their top
member.

**Accuracy rules**
- A lookup accepts an exact name/alias match, or a single partial match. "Store 1" never
  becomes "Store 117" silently. No match or an ambiguous match → the user is asked instead
  of getting numbers for the wrong slice.
- Only name-like words (a capital letter or a digit, or text in quotes) are looked up.
  Ordinary words are ignored.
- Ranking happens on the values Essbase returned; the follow-up query keeps that order.

## One shared connection, many users
```
100 browsers ──► web threads (EMCP_WEB_THREADS, default 128)
                    │  each request names its cube; no shared "current cube"
                    ▼
               shared contexts (parsed once per cube, reloaded when the file changes)
                    │
                    ▼
        ONE Runtime: token · MCP session · connection pool · call limiter
                    │  at most max_concurrent_calls in flight; others queue
                    ▼
                Essbase 26ai MCP
```

| Concern | How it's handled |
|---|---|
| Sign-in | One OAuth token for everyone. Refresh is **single-flight**: one thread fetches, the rest wait and reuse it. A 401 discards only the token that failed, then retries once. |
| MCP session | Initialized once. If the server drops it (404), **one** thread re-initializes and everyone moves to the new session. Each request remembers which session it used, so a late 404 can't trigger a second re-init. |
| Overload | `max_concurrent_calls` (default 20) caps parallel Essbase calls; the connection pool is sized to match. Extra calls wait up to `queue_timeout_s`, then the user gets HTTP 503 "busy, try again" rather than a timeout or a pile-up on Essbase. |
| Which cube | Chosen per browser and sent with every request. One user switching cubes never affects another. The admin's saved cube is only the default for new browsers. |
| Context edits | Saves carry the revision the editor started from. If someone saved in between, the save is refused (HTTP 409) and the UI offers to load their version. Files are written atomically. |
| Loading cubes | One load per cube at a time; different cubes load in parallel. |
| Member lookups | Results are remembered per cube for everyone (up to 5,000). Concurrent askers for the same unknown name wait for **one** search. |
| Settings change | A new connection is built and swapped in atomically. Questions already running finish on the old one. |

`GET /api/metrics` shows questions, Essbase calls, token fetches, session re-inits, peak
parallel calls, busy rejections, errors and call latency (p50/p95).

## Proving it
```bash
pytest tests/test_concurrency.py           # 100 threads, dropped sessions, expired tokens, overload
emcp web                                   # then, in another terminal:
python scripts/loadtest.py --users 100 --questions 5
EMCP_MOCK_LATENCY_MS=200 emcp web          # make the mock as slow as a real server
```
Sample run on a laptop with the mock at 200 ms per Essbase call:
```
100 users x 5 questions = 500 requests in 7.0s (71 req/s)
latency ms  p50 1030  p95 2052  p99 2720  max 4033
HTTP codes  {200: 500}   answered correctly: 500/500
server      token fetches 0, MCP session inits 0, Essbase calls 629, peak parallel 20/20, busy 0, errors 0
```
Throughput is bounded by `max_concurrent_calls ÷ Essbase latency` (20 ÷ 0.2 s ≈ 100 calls/s).
To go faster, raise the cap if your Essbase server has headroom.

## Limits of this reference
- **One identity for everyone.** With client credentials, Essbase sees the app, not the
  person, so security filters don't differ per user. For that, use per-user sign-in
  (authorization code + PKCE; on the roadmap). The concurrency design stays the same, with
  one token per user instead of one shared.
- **One process.** Counters, the lookup cache and the call limiter are per process. If you run
  several processes or containers, each has its own (the per-server cap becomes cap × processes).
  Contexts are shared through the files, and the revision check and atomic writes keep edits
  consistent. A tiny window remains between two processes saving at the same instant; put
  contexts in a database if that matters.
