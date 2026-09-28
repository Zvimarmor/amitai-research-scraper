#!/usr/bin/env bash
#
# Bring the research tool online:
#   API on :8000  ->  Cloudflare quick tunnel  ->  _redirects  ->  git push
#
# Netlify proxies the browser's /api/* calls to the tunnel, so the page stays
# same-origin and needs no CORS grant. A quick tunnel gets a fresh hostname on
# every start, which is why the redirect files are rewritten and pushed each run.
#
# Usage:  scripts/start_production.sh [--no-push]
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PORT="${API_PORT:-8000}"
PUSH=1
[[ "${1:-}" == "--no-push" ]] && PUSH=0

LOG_DIR="$ROOT/runs/production"
mkdir -p "$LOG_DIR"
API_LOG="$LOG_DIR/api.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"

API_PID=""
TUNNEL_PID=""

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '\033[1;31m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

cleanup() {
  log "shutting down"
  [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  [[ -n "$API_PID" ]] && kill "$API_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

[[ -x .venv/bin/python ]] || die ".venv not found - run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
command -v cloudflared >/dev/null || die "cloudflared not installed - run: brew install cloudflared"
[[ -f .env ]] || die ".env not found - copy .env.example and add GEMINI_API_KEY + API_AUTH_TOKEN"

# The tunnel makes this API reachable from the internet, so refuse to expose it
# with the fallback dev token still in place.
TOKEN="$(grep -E '^API_AUTH_TOKEN=' .env | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
[[ -n "$TOKEN" && "$TOKEN" != "local-dev-key" ]] \
  || die "API_AUTH_TOKEN is unset or the dev default; generate one:
  python -c 'import secrets; print(secrets.token_urlsafe(32))'"

# --------------------------------------------------------------------------
# 1. API
# --------------------------------------------------------------------------

if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
  die "port $PORT is already in use - stop the process first: lsof -ti tcp:$PORT | xargs kill"
fi

log "starting API on 127.0.0.1:$PORT"
API_PORT="$PORT" .venv/bin/python -m src.server >"$API_LOG" 2>&1 &
API_PID=$!

for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    break
  fi
  kill -0 "$API_PID" 2>/dev/null || { cat "$API_LOG"; die "API died on startup"; }
  sleep 0.5
done
curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null \
  || { tail -20 "$API_LOG"; die "API did not become healthy"; }
log "API healthy (pid $API_PID)"

# --------------------------------------------------------------------------
# 2. Tunnel
# --------------------------------------------------------------------------

log "opening Cloudflare quick tunnel"
: >"$TUNNEL_LOG"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

TUNNEL_URL=""
for _ in $(seq 1 60); do
  TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
  [[ -n "$TUNNEL_URL" ]] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || { tail -20 "$TUNNEL_LOG"; die "cloudflared exited"; }
  sleep 1
done
[[ -n "$TUNNEL_URL" ]] || { tail -20 "$TUNNEL_LOG"; die "no tunnel hostname after 60s"; }
log "tunnel: $TUNNEL_URL"

# Prove the tunnel actually reaches the API rather than trusting the log line.
for _ in $(seq 1 20); do
  curl -fsS --max-time 5 "$TUNNEL_URL/api/health" >/dev/null 2>&1 && break
  sleep 2
done
if curl -fsS --max-time 5 "$TUNNEL_URL/api/health" >/dev/null 2>&1; then
  log "tunnel verified end to end"
else
  warn "could not reach $TUNNEL_URL/api/health from this machine."
  warn "If tunnel.log says 'Registered tunnel connection', the tunnel is live and"
  warn "this is local DNS/egress filtering - confirm from a browser instead."
fi

# --------------------------------------------------------------------------
# 3. Redirects
# --------------------------------------------------------------------------

# Netlify only honours _redirects at the publish root, and which directory that
# is depends on the site's build settings - so write both candidates and keep
# netlify.toml in step, since a [[redirects]] rule there wins over the file.
RULE="/api/*  $TUNNEL_URL/api/:splat  200!"
CHANGED=()

for f in frontend/amitai/_redirects frontend/_redirects; do
  mkdir -p "$(dirname "$f")"
  if [[ ! -f "$f" ]] || [[ "$(cat "$f")" != "$RULE" ]]; then
    printf '%s\n' "$RULE" >"$f"
    CHANGED+=("$f")
  fi
done

> netlify/edge-functions/origin.ts cat <<EOF
// Rewritten by scripts/start_production.sh on every launch. Not a secret: the
// tunnel hostname is public, and the bearer token is what protects the API.
export const API_ORIGIN = "$TUNNEL_URL";
EOF
git diff --quiet -- netlify/edge-functions/origin.ts || CHANGED+=("netlify/edge-functions/origin.ts")

if [[ -f frontend/netlify.toml ]]; then
  .venv/bin/python - "$TUNNEL_URL" <<'PY'
import re, sys, pathlib
url = sys.argv[1]
p = pathlib.Path("frontend/netlify.toml")
s = p.read_text()
new = re.sub(r'(?m)^(\s*to\s*=\s*)".*?/api/:splat"', rf'\1"{url}/api/:splat"', s)
if new != s:
    p.write_text(new)
    print("netlify.toml updated")
PY
  git diff --quiet -- frontend/netlify.toml || CHANGED+=("frontend/netlify.toml")
fi

log "redirect target: $TUNNEL_URL/api/:splat"

# --------------------------------------------------------------------------
# 4. Commit + push
# --------------------------------------------------------------------------

if (( PUSH )); then
  git add -- frontend/amitai/_redirects frontend/_redirects \
             frontend/amitai/index.html frontend/netlify.toml \
             netlify/edge-functions/origin.ts 2>/dev/null || true
  if git diff --cached --quiet; then
    log "nothing to commit - redirect already points at this tunnel"
  else
    git commit -q -m "Update live tunnel endpoint and UI polish"
    if git push -q origin main 2>>"$LOG_DIR/git.log"; then
      log "pushed to origin/main ($(git rev-parse --short HEAD))"
    else
      warn "push failed - see $LOG_DIR/git.log; the tunnel is still up"
    fi
  fi
else
  log "--no-push: redirect files written but not committed"
fi

# --------------------------------------------------------------------------
# 5. Stay up
# --------------------------------------------------------------------------

cat <<EOF

  API     http://127.0.0.1:$PORT      (pid $API_PID)
  tunnel  $TUNNEL_URL   (pid $TUNNEL_PID)
  logs    $API_LOG
          $TUNNEL_LOG

  Paste the API token into the page sidebar under "חיבור לשרת" once.
  Ctrl-C stops both processes. A restart gets a NEW tunnel hostname and needs
  another push, so for a stable URL move to a named tunnel (see README).

EOF

# Exit as soon as either process dies, so a dead tunnel is never left looking healthy.
while true; do
  kill -0 "$API_PID"    2>/dev/null || { warn "API exited";    tail -20 "$API_LOG";    exit 1; }
  kill -0 "$TUNNEL_PID" 2>/dev/null || { warn "tunnel exited"; tail -20 "$TUNNEL_LOG"; exit 1; }
  sleep 5
done
