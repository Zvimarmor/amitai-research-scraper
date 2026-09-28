# Roadmap to Product — Technical Handover

Research engine for the study of **adolescent medical privacy in Israel**: the
tension between adolescent autonomy and parental involvement, in **Type 1
Diabetes** and **psychiatric / mental-health care**.

Status: the local engine is complete and verified end to end. The web layer is
built but deployment is deliberately paused. This document is the handover for
whoever runs, extends, or ships it next.

---

## 1. System state & architecture

### Pipeline

```
themes + prompt + keywords
   └─▶ searcher.py   build dorks (3 interleaved tiers) → ddgs metasearch
        └─▶ fetcher.py   curl_cffi Chrome impersonation → per-site parsers
             └─▶ screener.py   pass 1 regex co-occurrence → pass 2 LLM JSON verdict
                  └─▶ db.py    SQLite, deduped on url + content_hash
                       └─▶ CLI (main.py) or HTTP API (server.py) → CSV / JSON
```

### Module map

| File | Responsibility |
|---|---|
| `src/config.py` | Env-driven settings, all overridable from `.env` |
| `src/models.py` | `Post`, `ScrapingQuery`, `LLMAnalysisResult`, `RegexScreenResult`, `ScreenedPost` |
| `src/searcher.py` | Theme vocabulary → `site:` dorks; metasearch with per-query timeout |
| `src/fetcher.py` | TLS-impersonating fetch; FXP / Stips / Reddit parsers |
| `src/screener.py` | Hebrew-aware regex filter; Gemini/OpenAI structured classifier; query expansion |
| `src/db.py` | SQLite store, dedup, pagination, filtering |
| `src/main.py` | CLI: `run`, `rescreen`, `queries`, `show`, `stats` |
| `src/server.py` | FastAPI: background worker, status, posts, CSV export |
| `frontend/amitai/index.html` | Single-page console (Tailwind CDN + vanilla JS) |
| `tests/` | 46 offline tests + 7 live-LLM tests that skip without a key |

### Current capabilities

- **Sources:** Stips and FXP verified working live. Reddit is blocked at IP level (§2).
- **Hebrew-aware matching.** Clitic prefixes (`ו/ה/ב/ל/מ/ש/כ`, up to three) are
  handled on every word of a phrase, so `מידע רפואי` matches `המידע הרפואי`.
  Short terms are boundary-anchored so `נער` (youth) does not match inside
  `נערך` (*was edited*) — the footer on every vBulletin post, which otherwise
  made every FXP thread a false positive.
- **Two-stage screening.** The regex pass is free and removes most noise; only
  survivors reach the LLM, which is the only paid step.
- **Idempotent collection.** Re-running skips URLs and content hashes already
  stored, so runs can be interrupted and resumed freely.
- **Decoupled screening.** `rescreen` runs the LLM pass over stored posts, so
  collection (slow, no key needed) and screening (fast, paid) are independent,
  and the prompt can be revised without re-scraping.

### Database schema

`data/research.sqlite3`, single table:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `url` | TEXT **UNIQUE** | dedup key; drives the upsert |
| `content_hash` | TEXT, indexed | SHA-256 of whitespace/case-normalised full text; catches the same thread at a different URL |
| `source` | TEXT, indexed | `stips` / `fxp` / `reddit` / `other` |
| `title` | TEXT | |
| `raw_text` | TEXT | title + body + all comments, as screened |
| `query` | TEXT | the dork that surfaced it — provenance for the methods section |
| `regex_json` | TEXT | serialised `RegexScreenResult` (matched terms + reason) |
| `analysis_json` | TEXT | serialised `LLMAnalysisResult`, `NULL` until screened |
| `created_at` / `updated_at` | TEXT | UTC ISO-8601 |

Upserts use `COALESCE`, so re-scraping a post never erases an existing verdict.
`is_relevant` and `confidence` live inside `analysis_json`; those two filters are
applied in Python after the SQL-level ones. At this corpus size (thousands of
rows) that is irrelevant; past ~100k rows, promote them to real columns with an
index.

### Performance benchmarks

Measured on this Mac mini, residential IP, September 2026:

| Stage | Observed | Notes |
|---|---|---|
| Query generation | <10 ms for ~380 dorks | pure CPU |
| Search, per query | **5–8 s** | dominates every run; `ddgs` fans out across engines |
| Search phase | ~8 min for 95 queries → 9 unique URLs | Hebrew dorks are sparse |
| Fetch + parse, per post | **4–8 s** | includes the 2–4.5 s politeness delay |
| Regex screen | <1 ms/post | |
| LLM screen | **~3–4 s/post, ~900 tokens** (479 in / 115 out on a typical Stips thread) | plus the configured inter-call delay |
| End-to-end | **~10–15 min per 10 candidates** | search-bound, not CPU-bound |

**The search step is the bottleneck and it throttles.** Engines start returning
403/429 under sustained use. Budget roughly 15 minutes per 10 candidates, and
spread large collection campaigns across sessions rather than one long run.

### Test suite

```bash
.venv/bin/python -m pytest tests/ -q          # 46 offline tests, ~0.3 s
.venv/bin/python -m pytest tests/test_llm_live.py -v -s   # 7 live tests, needs a key
```

Offline coverage: Hebrew lexicon and clitic edge cases, the `נערך` false
positive, co-occurrence rules, JSON recovery from fenced/prefixed output, field
coercion, dedup (URL, content hash, `COALESCE` preservation), pagination and
filtering, all API endpoints, the worker state machine (start/stop/409/error
recovery), and CSV/BOM formatting. The scraper's network calls are not mocked —
site parsers are covered by HTML fixtures instead.

---

## 2. Unblocking Reddit

Reddit currently returns **403 on `www.reddit.com/*.json`** to browser-like
clients and serves an anti-bot interstitial on `old.reddit.com`. Both parsers
(`parse_reddit_json`, `parse_reddit_html`) are implemented and fixture-tested;
the fetcher detects the interstitial and returns `None` rather than storing
junk. What is missing is authenticated access.

The fix is a free **script app**, which raises the limit to 100 requests/minute:

1. Sign in to Reddit, go to <https://www.reddit.com/prefs/apps>.
2. **create another app…** → type **script** (not "web app"). Name it for the
   study; set redirect URI to `http://localhost:8080` (unused by script apps).
3. Copy the **client ID** (the string under the app name) and the **secret**.
4. Add to `.env`:
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_CLIENT_SECRET=...
   REDDIT_USER_AGENT=script:adolescent-privacy-research:v0.1 (by /u/YOURNAME)
   ```
   A descriptive user agent is required by Reddit's API rules; a generic one
   gets throttled.
5. Implement the token exchange in `fetcher.py` — POST
   `https://www.reddit.com/api/v1/access_token` with
   `grant_type=client_credentials`, HTTP-basic auth of id:secret, then call
   `https://oauth.reddit.com<permalink>` with `Authorization: bearer <token>`.
   Tokens last ~24 h; cache and refresh on 401. The existing
   `parse_reddit_json` consumes the response unchanged — only the transport
   needs to change.
6. Alternatively use **PRAW**, which handles auth and rate limiting, at the cost
   of one more dependency.

Respect Reddit's API terms: client-credentials access is read-only and
rate-limited, and Reddit's Data API terms restrict redistribution of content —
relevant when publishing quotes (§4).

---

## 3. Frontend & deployment packaging

### What exists

| Path | Purpose |
|---|---|
| `frontend/amitai/index.html` | The whole UI — one file, no build step, no npm |
| `frontend/_redirects` | Netlify proxy rule (one line) |
| `frontend/netlify.toml` | The same rule in TOML — use one, not both |

The page has: prompt box, keyword chips, AI-expansion toggle, theme/source
selectors, start/stop with a live status indicator, counters (scanned / regex
passed / LLM relevant / URLs found), a results feed with source badges,
confidence bars, RTL-aware titles and highlighted verbatim quotes, and a CSV
export button. The API base is editable in the UI and stored in `localStorage`.

### Three deployment options

**A. Local only (current, recommended while collecting).** Run
`python -m src.server` and open `frontend/amitai/index.html`, pointing the API
base at `http://127.0.0.1:8000/api`. No exposure, no tunnel, no CORS. Everything
needed for the research is available this way.

**B. Netlify + Cloudflare Tunnel.** The page is served from Netlify at
`/amitai`; `/api/*` is proxied to the Mac mini through a tunnel, so calls are
same-origin and CORS never applies.

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8000     # quick, hostname changes on restart
```
For a stable hostname: `cloudflared tunnel login && cloudflared tunnel create
amitai-api && cloudflared tunnel route dns amitai-api api.zvimarmor.com`, which
requires that subdomain to be on Cloudflare DNS. Then one line in `_redirects`
at the publish root:
```
/api/*  https://YOUR-TUNNEL-HOSTNAME/api/:splat  200
```
Namecheap DNS needs no change, because the browser only ever talks to Netlify.

Before exposing anything: set `ALLOWED_ORIGINS=https://zvimarmor.com` (the
default `*` is for local development), and **add authentication** — the API has
none, and anyone with the tunnel URL could start runs from your IP and read the
corpus. Cloudflare Access in front of the tunnel is the least-effort answer.

**C. VPS container.** Only worth it if the tool must run while the Mac mini is
off. Note this moves scraping to a datacenter IP, which is blocked far more
aggressively than a residential one — expect *worse* yields. Mount SQLite on a
persistent volume; a `Dockerfile` does not yet exist.

### Environment configuration

All of `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `openai` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — / `gemini-3.6-flash` | |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | — / `gpt-4o-mini` | |
| `FETCH_MIN_DELAY` / `FETCH_MAX_DELAY` | `2.0` / `4.5` | Politeness window, seconds |
| `SEARCH_DELAY` / `SEARCH_TIMEOUT` | `3.0` / `45.0` | Between queries; per-query deadline |
| `SEARCH_BACKEND` / `SEARCH_REGION` | `auto` / `il-he` | ddgs engines; result region |
| `DB_PATH` | `data/research.sqlite3` | |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | |
| `ALLOWED_ORIGINS` / `ALLOWED_ORIGIN_REGEX` | `*` / unset | CORS; narrow before exposing |

Do not lower the delay settings. They are what keeps the collector from being
blocked, and they keep load on the forums negligible.

---

## 4. Ethical & academic research checklist

The corpus is public but written largely by **minors about their own physical
and mental health**. Public does not mean consented-to-research.

### Before collecting

- [ ] IRB / ethics committee approval covering secondary use of public
      user-generated content authored by minors. Many committees treat this as
      exempt secondary-data research; confirm rather than assume.
- [ ] Review each site's terms of use and `robots.txt` (Stips, FXP, Reddit) and
      record the review date in the methods section.
- [ ] Decide and document the retention period and deletion plan.

### Storage

- [ ] `data/` is gitignored. Treat the SQLite file as **identifiable data**: it
      stores full post text, URLs, and sometimes usernames.
- [ ] Keep it on an encrypted volume (FileVault). Do not sync to consumer cloud
      storage.
- [ ] Never commit the DB, exports, or `.env`.

### Anonymisation before any sharing or publication

- [ ] Strip usernames and author handles.
- [ ] Replace direct URLs with opaque study IDs in published material. Keep the
      URL↔ID mapping in a separate, access-controlled file.
- [ ] **Verbatim quotes are re-identifiable.** A distinctive Hebrew sentence can
      be pasted into a search engine and traced back to its author. Paraphrase,
      translate, or aggregate quotes in publications; reserve verbatim text for
      the analysis stage only.
- [ ] Redact incidental identifiers inside quoted text — school names, clinics,
      towns, treating physicians, rare diagnoses, exact dates.
- [ ] Apply extra care to psychiatric-care posts: disclosure risk there is
      materially higher than for diabetes posts.

### Analysis and export

- CSV export is **UTF-8 with BOM**, so Excel opens Hebrew columns correctly
  without an import wizard.
- Columns: `id, source, title, url, created_at, is_relevant, topic,
  privacy_tension, confidence, summary, key_quotes, medical_terms,
  privacy_terms, query`. Multi-valued fields are ` | `-joined.
- **ATLAS.ti / MAXQDA:** import the CSV as a *document table* — one row per
  document, with `raw_text` as the content column and the rest as document
  variables, letting you filter by `topic`, `privacy_tension` and `confidence`
  before coding. To export `raw_text` too, add it to `CSV_COLUMNS` in
  `server.py`; it is omitted by default to keep exports shareable.
- **Reliability.** The LLM verdict is a *screening aid, not a finding*.
  Hand-code a random sample (≥50 posts) and report agreement with the model
  (Cohen's κ) in the methods section. `confidence` is the model's self-report and
  is not calibrated — do not treat it as a probability.
- Record the `query` column in the methods section: it is the audit trail of how
  each post was found.

---

## 5. Quick-start command reference

```bash
# Setup
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add GEMINI_API_KEY

# Tests
.venv/bin/python -m pytest tests/ -q                      # 46 offline tests
.venv/bin/python -m pytest tests/test_llm_live.py -v -s   # live LLM check

# Inspect queries without touching the network
.venv/bin/python -m src.main queries --themes t1d psychiatric

# Case A — Type 1 Diabetes & autonomy
.venv/bin/python -m src.main run --themes t1d --sites stips.co.il fxp.co.il \
  --limit 14 --keywords "סוכרת בלי שההורים ידעו" "לא מודד סוכר" "מזריק אינסולין לבד" -v

# Case B — psychiatric care & confidentiality
.venv/bin/python -m src.main run --themes psychiatric --sites stips.co.il fxp.co.il \
  --limit 14 --keywords "טיפול פסיכולוגי בלי אישור הורים" "פסיכיאטר מספר להורים" -v

# Collect now, screen later (no API key needed for the collection half)
.venv/bin/python -m src.main run --limit 20 --no-llm
.venv/bin/python -m src.main rescreen --limit 100

# Read back
.venv/bin/python -m src.main stats
.venv/bin/python -m src.main show --relevant

# Local API + UI
.venv/bin/python -m src.server            # http://127.0.0.1:8000 , docs at /docs
open frontend/amitai/index.html           # set API base to http://127.0.0.1:8000/api

# Export
curl -o screened.csv "http://127.0.0.1:8000/api/posts/export?relevant_only=true"
```

Useful `run` flags: `--expand` (LLM query expansion), `--no-llm` (regex only),
`--require-adolescent` (demand an age marker), `--strict-queries` (precision
tier only), `--provider`, `--backend`.

---

## 6. Known limitations & next steps

1. **Reddit is blocked** — §2. Biggest single yield improvement available.
2. **Search throttling** caps throughput at roughly 10 candidates per 15
   minutes. A paid search API (Brave, Serper, Google CSE) would remove the
   bottleneck and make runs deterministic; this is the highest-value paid
   upgrade.
3. **No authentication on the API.** Mandatory before any tunnel (§3).
3b. **Model pinning.** `gemini-2.0-flash` was retired mid-development and the API
   returned 404 with a pointer to its successor. `GEMINI_MODEL` is configurable
   for exactly this reason — if screening starts 404-ing, list available models
   (`client.models.list()`) and update `.env` rather than editing code. Note also
   that the JSON schema is filtered per provider: OpenAI strict mode requires
   `additionalProperties: false`, which Gemini rejects outright.
4. **Site selectors will drift.** FXP and Stips markup changes break parsers
   silently — yields drop rather than errors appear. If a run returns posts with
   near-empty bodies, update `*_SELECTORS` in `fetcher.py` first.
5. **Single-run worker.** One collection at a time by design; parallel runs would
   multiply the block risk.
6. **No date filtering.** Stips and FXP threads span a decade. If recency
   matters, add a `posted_at` column and parse it per site.
7. **LLM verdicts are unvalidated** against human coding — see §4.
