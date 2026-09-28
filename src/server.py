"""FastAPI wrapper around the collection engine.

One collection run at a time, on a background worker thread. The engine is
synchronous and network-bound, so a thread (not an asyncio task) keeps the event
loop free to serve status polls while a run is in progress.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .db import Database
from .main import run_pipeline, setup_logging
from .searcher import DEFAULT_SITES, THEMES

log = logging.getLogger("server")

MAX_FEED = 200  # most recent results kept in memory for the live feed


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------

class StartRequest(BaseModel):
    prompt: str = ""
    keywords: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=lambda: ["t1d", "psychiatric"])
    sites: list[str] = Field(default_factory=lambda: list(DEFAULT_SITES))
    limit: int = Field(default=10, ge=1, le=200)
    expand: bool = False
    use_llm: bool = True
    require_adolescent: bool = False
    provider: Optional[str] = None
    backend: Optional[str] = None


class StatusResponse(BaseModel):
    state: str
    prompt: str = ""
    scanned: int = 0
    regex_passed: int = 0
    llm_relevant: int = 0
    errors: int = 0
    queries: int = 0
    urls_found: int = 0
    expanded_terms: list[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    stop_requested: bool = False
    message: str = ""
    feed: list[dict] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Row shaping - the live feed and /api/posts must hand the UI the same shape
# --------------------------------------------------------------------------

def _shape(row: dict) -> dict:
    """Flatten a stored row into what the UI needs."""
    analysis = row.get("analysis_json") or {}
    regex = row.get("regex_json") or {}
    return {
        "id": row.get("id"),
        "url": row.get("url"),
        "source": row.get("source"),
        "title": row.get("title") or "",
        "query": row.get("query") or "",
        "created_at": row.get("created_at"),
        "regex_passed": bool(regex.get("passed")),
        "medical_terms": regex.get("medical_terms", []),
        "privacy_terms": regex.get("privacy_terms", []),
        "is_relevant": analysis.get("is_relevant"),
        "topic": analysis.get("topic"),
        "privacy_tension": analysis.get("privacy_tension"),
        "confidence": analysis.get("confidence"),
        "summary": analysis.get("summary"),
        "key_quotes": analysis.get("key_quotes", []),
        "analyzed": bool(analysis),
    }


def _shape_event(payload: dict) -> dict:
    """Adapt a live pipeline event to the same shape `_shape` produces."""
    return _shape({
        "url": payload.get("url"),
        "source": payload.get("source"),
        "title": payload.get("title"),
        "query": payload.get("query"),
        "regex_json": payload.get("regex_screen") or {},
        "analysis_json": payload.get("analysis") or {},
    }) | {"error": payload.get("error")}


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

class Worker:
    """Owns the single background run and its progress counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"
        self.prompt = ""
        self.scanned = 0
        self.regex_passed = 0
        self.llm_relevant = 0
        self.errors = 0
        self.queries = 0
        self.urls_found = 0
        self.expanded_terms: list[str] = []
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.message = ""
        self.feed: list[dict] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> StatusResponse:
        with self._lock:
            return StatusResponse(
                state=self.state,
                prompt=self.prompt,
                scanned=self.scanned,
                regex_passed=self.regex_passed,
                llm_relevant=self.llm_relevant,
                errors=self.errors,
                queries=self.queries,
                urls_found=self.urls_found,
                expanded_terms=list(self.expanded_terms),
                started_at=self.started_at,
                finished_at=self.finished_at,
                stop_requested=self._stop.is_set(),
                message=self.message,
                feed=list(reversed(self.feed[-MAX_FEED:])),
            )

    def start(self, req: StartRequest) -> None:
        with self._lock:
            if self.running:
                raise HTTPException(409, "A collection run is already in progress")
            self._reset()
            self._stop.clear()
            self.state = "running"
            self.prompt = req.prompt
            self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._thread = threading.Thread(target=self._run, args=(req,), daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Flag the run to halt. It finishes the in-flight request first."""
        was_running = self.running
        if was_running:
            self._stop.set()
            with self._lock:
                self.state = "stopping"
                self.message = "Stop requested; finishing the current request."
        return was_running

    def _on_event(self, kind: str, payload: dict) -> None:
        with self._lock:
            if kind == "expanded":
                self.expanded_terms = payload.get("terms", [])
            elif kind == "searching":
                self.queries = payload.get("queries", 0)
                self.message = f"Searching ({self.queries} queries generated)"
            elif kind == "search_done":
                self.urls_found = payload.get("urls", 0)
                self.message = f"Screening {self.urls_found} candidate URLs"
            elif kind == "post":
                self.scanned += 1
                if (payload.get("regex_screen") or {}).get("passed"):
                    self.regex_passed += 1
                analysis = payload.get("analysis") or {}
                if analysis.get("is_relevant"):
                    self.llm_relevant += 1
                if payload.get("error"):
                    self.errors += 1
                self.feed.append(_shape_event(payload))
                self.feed = self.feed[-MAX_FEED:]

    def _run(self, req: StartRequest) -> None:
        try:
            run_pipeline(
                prompt=req.prompt,
                themes=req.themes,
                sites=req.sites,
                limit=req.limit,
                use_llm=req.use_llm,
                require_adolescent=req.require_adolescent,
                provider=req.provider,
                backend=req.backend,
                keywords=req.keywords,
                expand=req.expand,
                should_stop=self._stop.is_set,
                on_event=self._on_event,
            )
            final, msg = "stopped" if self._stop.is_set() else "done", ""
        except Exception as exc:  # a failed run must not wedge the worker
            log.exception("Collection run failed")
            final, msg = "error", str(exc)
        with self._lock:
            self.state = final
            self.message = msg or self.message or f"Run {final}."
            self.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


worker = Worker()
db = Database()

app = FastAPI(
    title="Adolescent Medical Privacy Research API",
    description="Local collection engine for the Israeli adolescent medical-privacy study.",
    version="0.1.0",
)

# The frontend is served from Netlify, so the browser calls this API cross-origin.
# ALLOWED_ORIGINS is a comma-separated list, strictly enforced (no "*" default -
# a tunnel exposes this to the internet). Localhost is always allowed so local
# frontend dev keeps working without touching .env. ALLOWED_ORIGIN_REGEX is an
# optional extra pattern, e.g. for Netlify deploy previews:
#   ALLOWED_ORIGIN_REGEX=https://.*--your-site\.netlify\.app
_DEFAULT_ORIGINS = "https://zvimarmor.com"
_LOCAL_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:5500", "http://127.0.0.1:5500"]
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]
if "*" in _origins:
    log.warning("ALLOWED_ORIGINS=* allows any site to call this API - do not use this behind a tunnel")
else:
    _origins = sorted(set(_origins) | set(_LOCAL_ORIGINS))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=os.getenv("ALLOWED_ORIGIN_REGEX") or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Auth - a single shared bearer token. Good enough for a one-user local API
# exposed through a tunnel; not a substitute for real per-user auth.
# --------------------------------------------------------------------------

_DEV_TOKEN = "local-dev-key"
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "").strip() or _DEV_TOKEN
if API_AUTH_TOKEN == _DEV_TOKEN:
    log.warning("API_AUTH_TOKEN not set - using the insecure default dev key; set it before exposing a tunnel")


def require_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not token or token != API_AUTH_TOKEN:
        raise HTTPException(401, "Missing or invalid API token")


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "state": worker.state, "themes": sorted(THEMES), "sites": list(DEFAULT_SITES)}


@app.post("/api/search/start", response_model=StatusResponse)
def start_search(req: StartRequest, _auth: None = Depends(require_auth)) -> StatusResponse:
    worker.start(req)
    return worker.status()


@app.post("/api/search/stop", response_model=StatusResponse)
def stop_search(_auth: None = Depends(require_auth)) -> StatusResponse:
    if not worker.stop():
        log.info("Stop requested with no run in progress")
    return worker.status()


@app.get("/api/search/status", response_model=StatusResponse)
def search_status(_auth: None = Depends(require_auth)) -> StatusResponse:
    return worker.status()


@app.get("/api/posts")
def list_posts(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    relevant_only: bool = True,
    analyzed_only: bool = False,
    source: Optional[str] = None,
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    search: Optional[str] = None,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    page = db.fetch_posts(
        limit=limit,
        offset=offset,
        relevant_only=relevant_only,
        analyzed_only=analyzed_only,
        source=source,
        min_confidence=min_confidence,
        search=search,
    )
    return {
        "total": page["total"],
        "limit": limit,
        "offset": offset,
        "items": [_shape(r) for r in page["items"]],
    }


CSV_COLUMNS = [
    "id", "source", "title", "url", "created_at", "is_relevant", "topic",
    "privacy_tension", "confidence", "summary", "key_quotes",
    "medical_terms", "privacy_terms", "query",
]


@app.get("/api/posts/export")
def export_posts(
    relevant_only: bool = True,
    analyzed_only: bool = False,
    source: Optional[str] = None,
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    search: Optional[str] = None,
    _auth: None = Depends(require_auth),
) -> StreamingResponse:
    page = db.fetch_posts(
        limit=0, relevant_only=relevant_only, analyzed_only=analyzed_only,
        source=source, min_confidence=min_confidence, search=search,
    )
    buf = io.StringIO()
    # utf-8-sig via the BOM below so Excel opens the Hebrew columns correctly.
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in page["items"]:
        item = _shape(row)
        for key in ("key_quotes", "medical_terms", "privacy_terms"):
            item[key] = " | ".join(item.get(key) or [])
        writer.writerow(item)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="screened-posts-{stamp}.csv"'},
    )


@app.get("/api/stats")
def stats(_auth: None = Depends(require_auth)) -> dict:
    return db.stats()


def main() -> None:  # pragma: no cover - entry point
    import uvicorn

    setup_logging(verbose=False)
    uvicorn.run(
        app,
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
