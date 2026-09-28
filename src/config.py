"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# gemini-2.0-flash was retired by Google (404 as of 2026-09-28, pointing here
# instead); gemini-3.8-flash is the current equivalent and is what every
# screening call uses unless GEMINI_MODEL overrides it.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

FETCH_MIN_DELAY = _float("FETCH_MIN_DELAY", 2.0)
FETCH_MAX_DELAY = _float("FETCH_MAX_DELAY", 4.5)
SEARCH_DELAY = _float("SEARCH_DELAY", 3.0)
REQUEST_TIMEOUT = _float("REQUEST_TIMEOUT", 30.0)

_db_path = os.getenv("DB_PATH", "data/research.sqlite3")
DB_PATH = Path(_db_path) if os.path.isabs(_db_path) else PROJECT_ROOT / _db_path

IMPERSONATE = os.getenv("IMPERSONATE", "chrome")

# ddgs fans out across search engines and enables/disables them dynamically, so
# the available set differs between calls and a hard-coded list goes stale.
# "auto" is therefore the default; narrow it with --backend (e.g. "brave, google")
# when a run is slow, and Searcher falls back to "auto" if the names are rejected.
SEARCH_BACKEND = os.getenv("SEARCH_BACKEND", "auto")
SEARCH_REGION = os.getenv("SEARCH_REGION", "il-he")
SEARCH_TIMEOUT = _float("SEARCH_TIMEOUT", 45.0)

SUPPORTED_SITES = ("fxp.co.il", "stips.co.il", "reddit.com")
