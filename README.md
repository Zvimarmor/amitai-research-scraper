# Adolescent Medical Privacy Research Platform

An automated academic research platform for investigating how Israeli youth
discuss medical privacy, confidentiality, and parental monitoring across public
online forums (Stips, FXP). The study question is the tension between adolescent
autonomy and parental involvement, in **Type 1 Diabetes** and **psychiatric /
mental-health care**.

**How it works**

- **Collection** — scans public youth discussions for intersections between
  medical topics (mental health, Type 1 Diabetes, medications) and parental
  boundaries.
- **Two-stage filtering** — a targeted keyword/regex co-occurrence gate, then
  Gemini screening to verify genuine privacy friction and exclude irrelevant
  posts.
- **Research analysis** — tags key quotes, assesses privacy tension, and exports
  structured CSV datasets for qualitative analysis.

**On privacy.** Every discussion collected was posted publicly. Source links and
verbatim quotes are retained deliberately, because a qualitative finding has to
be traceable back to its source to be verifiable — which means the corpus and its
exports are **identifiable data, not anonymised**. Identifying details must be
removed before any publication or sharing; see the checklist in
[ROADMAP_TO_PRODUCT.md](ROADMAP_TO_PRODUCT.md). The database and CSVs are
gitignored and should stay on an encrypted volume.

```
themes ──▶ searcher (DDG dorks) ──▶ fetcher (curl_cffi) ──▶ screener ──▶ db (SQLite)
                                                            ├─ pass 1: regex co-occurrence
                                                            └─ pass 2: LLM structured JSON
```

## Layout

| File | Role |
|---|---|
| `src/config.py`   | Env-driven settings (`.env`) |
| `src/models.py`   | `Post`, `ScrapingQuery`, `LLMAnalysisResult`, `RegexScreenResult`, `ScreenedPost` |
| `src/db.py`       | SQLite store, dedup on `url` + `content_hash` |
| `src/searcher.py` | Theme vocabulary → `site:` dorks → DuckDuckGo |
| `src/fetcher.py`  | Chrome-impersonating fetch + FXP / Stips / Reddit parsers |
| `src/screener.py` | Regex first pass, Gemini/OpenAI structured second pass |
| `src/main.py`     | CLI (`run`, `rescreen`, `queries`, `show`, `stats`) |
| `src/server.py`   | FastAPI wrapper: background worker, status, posts, CSV export |
| `frontend/amitai/index.html` | Single-page console (Tailwind CDN + vanilla JS) |
| `tests/test_smoke.py` | Parser/DB fixtures — no network, no API key |
| `tests/test_units.py` | Hebrew matching, clitics, dedup, pagination edge cases |
| `tests/test_api.py`   | FastAPI endpoints + worker state machine |
| `tests/test_llm_live.py` | Live LLM checks; auto-skipped without an API key |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env     # then add GEMINI_API_KEY (or OPENAI_API_KEY)
```

## Use

```bash
# inspect the generated dorks without hitting the network
.venv/bin/python -m src.main queries --themes t1d psychiatric

# test suite (46 offline tests, no network or API key)
.venv/bin/python -m pytest tests/ -q

# live LLM checks (needs GEMINI_API_KEY or OPENAI_API_KEY)
.venv/bin/python -m pytest tests/test_llm_live.py -v -s

# full pipeline: 10 candidates, LLM screening, JSON to stdout
.venv/bin/python -m src.main run --prompt "סודיות רפואית" --limit 10 -v

# regex-only dry run (no API key needed)
.venv/bin/python -m src.main run --limit 5 --no-llm

# collect now without an API key, run the LLM pass later
.venv/bin/python -m src.main run --limit 20 --no-llm
.venv/bin/python -m src.main rescreen --limit 100

# read back what was stored
.venv/bin/python -m src.main show --relevant
.venv/bin/python -m src.main stats
```

Query generation emits three tiers — precise (`site:X "clinical" "privacy"`),
loose (modifier unquoted), and bare (`site:X "clinical"`) — **interleaved**, not
concatenated. Two quoted Hebrew phrases rarely co-occur, so most precise dorks
return nothing while still costing a request; run in tier order they would burn
a whole run before reaching a productive query. Round-robin keeps precise dorks
first *and* reaches yielding ones within the first few requests. The regex and
LLM passes carry the precision burden. `--strict-queries` drops tiers 2–3.

`run` flags: `--themes {t1d,psychiatric,privacy,autonomy}`, `--sites`, `--limit`,
`--no-llm`, `--require-adolescent`, `--strict-queries`, `--provider {gemini,openai}`.

## Web tool

### 1. Run the API on the Mac mini

```bash
.venv/bin/python -m src.server          # http://127.0.0.1:8000, docs at /docs
```

Environment (`.env`): `API_HOST`, `API_PORT`, `API_AUTH_TOKEN`, `ALLOWED_ORIGINS`
(comma-separated, strictly enforced, default `https://zvimarmor.com`; localhost
dev origins are always allowed on top of it), and optionally
`ALLOWED_ORIGIN_REGEX` for Netlify deploy previews.

**Auth.** Every endpoint except `/api/health` requires a bearer token:

```bash
curl -H "Authorization: Bearer $API_AUTH_TOKEN" http://127.0.0.1:8000/api/stats
```

Set `API_AUTH_TOKEN` in `.env` — generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`. If it is unset
the server falls back to the insecure `local-dev-key` and logs a warning on
startup; never expose a tunnel in that state. In the web UI, paste the same
token under **חיבור לשרת** in the sidebar (it is kept in `localStorage`).

`/api/health` stays open deliberately, so a tunnel or uptime check can confirm
the process is up without holding the secret.

| Endpoint | Purpose |
|---|---|
| `POST /api/search/start` | Start a run: `prompt`, `keywords[]`, `themes[]`, `sites[]`, `limit`, `expand`, `use_llm`. 409 if one is already running. |
| `POST /api/search/stop` | Flag the worker to halt; it finishes the in-flight request, then reports `stopped`. |
| `GET  /api/search/status` | `state`, `scanned`, `regex_passed`, `llm_relevant`, `urls_found`, `expanded_terms`, plus the live results feed. |
| `GET  /api/posts` | Stored results, paginated (`limit`, `offset`) and filtered (`relevant_only`, `source`, `min_confidence`, `search`). |
| `GET  /api/posts/export` | Same filters, downloaded as UTF-8 BOM CSV (opens cleanly in Excel with Hebrew). |
| `GET  /api/health` | Liveness — the one endpoint that needs no token. |
| `GET  /api/stats` | Row counts (total / analyzed / relevant). |

One run at a time, on a daemon thread — the engine is synchronous and
network-bound, so a thread keeps the event loop free to answer status polls
mid-run. Stop is cooperative: the flag is polled between each search query and
each fetch, so nothing is killed mid-request.

### 2. One-command launch (API + tunnel + deploy)

```bash
scripts/start_production.sh            # add --no-push to skip the git commit
```

Starts the API, opens a Cloudflare quick tunnel, waits for both to be healthy,
rewrites `frontend/_redirects`, `frontend/amitai/_redirects` and the
`netlify.toml` rule to the new hostname, then commits and pushes so Netlify
redeploys. It refuses to start if `API_AUTH_TOKEN` is still the dev default,
since the tunnel exposes the API to the internet. Ctrl-C stops both processes;
logs are under `runs/production/`.

A **quick tunnel gets a new hostname every restart**, so each run produces
another commit. For a stable hostname use a named tunnel (below) and the
redirect files stop changing.

### 2b. Tunnel it manually

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8000     # prints https://<random>.trycloudflare.com
```

That quick tunnel is fine for testing but its hostname changes on every restart.
For a stable one:

```bash
cloudflared tunnel login
cloudflared tunnel create amitai-api
cloudflared tunnel route dns amitai-api api.zvimarmor.com
cloudflared tunnel run --url http://127.0.0.1:8000 amitai-api
```

DNS for `api.zvimarmor.com` is created by cloudflared in Cloudflare — so that
subdomain must be on Cloudflare. If `zvimarmor.com` stays on Namecheap DNS, use
the `trycloudflare.com` hostname directly in `_redirects` instead; nothing needs
to change on Namecheap either way, because the browser only ever talks to
Netlify.

### 3. Deploy the page

Copy `frontend/amitai/` to your Netlify site as `/amitai`, and add the redirect
(one line, `_redirects` at the publish root) with your tunnel hostname:

```
/api/*  https://YOUR-TUNNEL-HOSTNAME/api/:splat  200
```

`frontend/netlify.toml` holds the same rule in TOML form — use one, not both.
The proxy makes the browser's calls same-origin, so CORS is bypassed entirely
and no backend hostname is baked into the page. The API base is also editable in
the UI (stored in `localStorage`), which is how you point it straight at a
tunnel or at `http://127.0.0.1:8000/api` for local work.

**The page is only as available as the Mac mini.** When it is asleep or the
tunnel is down, the console shows `offline` and collection cannot start.

## Screening contract

`LLMAnalysisResult`: `is_relevant`, `topic` (`t1d|psychiatric|both|other`),
`privacy_tension`, `confidence` (0–1), `summary`, `key_quotes[]`. The provider is
asked for strict JSON against a schema; fenced or prefixed output is recovered,
and failures retry with exponential backoff before being recorded per-post in
the `error` field rather than aborting the run.

## Operational notes

- **Search is the bottleneck, and it throttles.** `ddgs` fans out over several
  engines per query and enables/disables them dynamically; engines return 403/429
  under sustained use, and a throttled one can hang. Each query therefore runs on
  a worker thread under a hard `SEARCH_TIMEOUT` (45 s), an unknown `--backend`
  falls back to `auto`, and "no results" is logged at debug rather than as a
  failure. Expect roughly 5–10 s per query, more once an IP is hot; spread large
  collection runs out over time rather than hammering.
- **Pacing.** Randomised 2–4.5 s between fetches, ~3 s between searches, and
  explicit backoff on 429/403/503. Don't remove these — they are what keeps the
  run from being blocked, and they keep load on the forums negligible.
- **Selectors drift.** FXP and Stips markup changes; each parser tries a list of
  candidate selectors and falls back to generic body text. If yields drop,
  update the `*_SELECTORS` lists in `fetcher.py` first.
- **Reddit is currently blocked from this machine.** `www.reddit.com/*.json`
  answers 403 to browser-like clients, and `old.reddit.com` serves an anti-bot
  interstitial instead of content. The fetcher sniffs for both and returns
  `None` with an explanatory log line rather than storing junk. Both the JSON
  and old.reddit HTML parsers are implemented and unit-tested against fixtures,
  so the path works from a network Reddit does not block, or once Reddit OAuth
  credentials are added. **FXP and Stips — the primary Israeli sources — work
  now and were verified live.**
- **Idempotent.** Re-running skips URLs and content hashes already stored.

## Research ethics

The corpus is public but written largely by minors about their own health. Before
using this beyond a technical test, confirm with your IRB / ethics committee; check
each site's terms of use; store the DB as identifiable data (`data/` is gitignored);
and strip usernames and quoted identifiers before anything is published. `key_quotes`
are verbatim and therefore re-identifiable via search — treat them as sensitive.
