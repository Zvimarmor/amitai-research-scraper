"""Integration tests for the FastAPI layer.

The engine itself is stubbed: these tests cover the HTTP contract, the worker
state machine, and export formatting - not the network-bound scraper.
"""
from __future__ import annotations

import csv
import io
import threading
import time

import pytest
from fastapi.testclient import TestClient

from src import server
from src.db import Database
from src.models import LLMAnalysisResult, Post, RegexScreenResult, Source


TEST_TOKEN = "test-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client backed by a throwaway SQLite file rather than the real corpus.

    Carries the bearer token by default so each test exercises its own subject
    rather than re-asserting auth; the auth tests below pass headers explicitly.
    """
    db = Database(tmp_path / "api.sqlite3")
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "API_AUTH_TOKEN", TEST_TOKEN)
    server.worker = server.Worker()
    with TestClient(server.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c, db


def _store(db, i: int, relevant: bool = True, source: Source = Source.STIPS,
           confidence: float = 0.9, quotes: list[str] | None = None):
    db.upsert_post(
        Post(url=f"https://stips.co.il/ask/{i}", source=source,
             title=f"שאלה מספר {i} על סודיות רפואית",
             body="גוף השאלה עם מספיק טקסט כדי להישמר במסד הנתונים"),
        regex=RegexScreenResult(passed=True, medical_terms=["טיפול נפשי"],
                                privacy_terms=["הורים"]),
        analysis=LLMAnalysisResult(
            is_relevant=relevant, topic="psychiatric", privacy_tension=relevant,
            confidence=confidence, summary=f"Summary {i}",
            key_quotes=quotes if quotes is not None else ["בלי שההורים ידעו"]),
    )


# --------------------------------------------------------------------------
# Health / status
# --------------------------------------------------------------------------

def test_health(client):
    c, _ = client
    body = c.get("/api/health").json()
    assert body["ok"] is True
    assert "t1d" in body["themes"] and "fxp.co.il" in body["sites"]


def test_status_starts_idle(client):
    c, _ = client
    body = c.get("/api/search/status").json()
    assert body["state"] == "idle"
    assert body["scanned"] == 0 and body["feed"] == []


def test_cors_preflight_is_allowed(client):
    c, _ = client
    r = c.options("/api/search/start", headers={
        "Origin": "https://zvimarmor.com",
        "Access-Control-Request-Method": "POST",
    })
    assert r.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in r.headers}


# --------------------------------------------------------------------------
# Worker lifecycle
# --------------------------------------------------------------------------

def test_start_runs_pipeline_and_reports_counters(client, monkeypatch):
    c, _ = client
    seen = {}

    def fake_pipeline(**kwargs):
        seen.update(kwargs)
        emit = kwargs["on_event"]
        emit("expanded", {"terms": ["בלי שההורים ידעו"]})
        emit("searching", {"queries": 42})
        emit("search_done", {"urls": 7})
        emit("post", {"url": "https://stips.co.il/ask/1", "source": "stips",
                      "title": "t", "regex_screen": {"passed": True},
                      "analysis": {"is_relevant": True, "confidence": 0.8,
                                   "summary": "s", "key_quotes": ["q"]}})
        emit("post", {"url": "https://stips.co.il/ask/2", "source": "stips",
                      "title": "t2", "regex_screen": {"passed": False},
                      "analysis": None, "error": "boom"})
        return []

    monkeypatch.setattr(server, "run_pipeline", fake_pipeline)
    r = c.post("/api/search/start", json={"prompt": "p", "keywords": ["k"],
                                          "limit": 5, "expand": True})
    assert r.status_code == 200

    for _ in range(100):
        body = c.get("/api/search/status").json()
        if body["state"] == "done":
            break
        time.sleep(0.02)

    assert body["state"] == "done"
    assert body["scanned"] == 2
    assert body["regex_passed"] == 1
    assert body["llm_relevant"] == 1
    assert body["errors"] == 1
    assert body["queries"] == 42 and body["urls_found"] == 7
    assert body["expanded_terms"] == ["בלי שההורים ידעו"]
    assert body["finished_at"]
    # The request's parameters actually reach the engine.
    assert seen["prompt"] == "p" and seen["keywords"] == ["k"] and seen["expand"] is True


def test_feed_matches_the_posts_payload_shape(client, monkeypatch):
    """The UI renders both with one function, so the keys must agree."""
    c, db = client

    def fake_pipeline(**kwargs):
        kwargs["on_event"]("post", {
            "url": "https://stips.co.il/ask/1", "source": "stips", "title": "t",
            "query": "q", "regex_screen": {"passed": True, "medical_terms": [],
                                           "privacy_terms": []},
            "analysis": {"is_relevant": True, "topic": "t1d", "privacy_tension": True,
                         "confidence": 0.7, "summary": "s", "key_quotes": ["q1"]}})
        return []

    monkeypatch.setattr(server, "run_pipeline", fake_pipeline)
    c.post("/api/search/start", json={"limit": 1})
    for _ in range(100):
        status = c.get("/api/search/status").json()
        if status["state"] == "done":
            break
        time.sleep(0.02)

    _store(db, 1)
    feed_keys = set(status["feed"][0]) - {"error"}
    post_keys = set(c.get("/api/posts").json()["items"][0])
    assert feed_keys == post_keys


def test_second_start_while_running_is_rejected(client, monkeypatch):
    c, _ = client
    release = threading.Event()
    monkeypatch.setattr(server, "run_pipeline",
                        lambda **kw: release.wait(timeout=5))
    try:
        assert c.post("/api/search/start", json={"limit": 1}).status_code == 200
        assert c.post("/api/search/start", json={"limit": 1}).status_code == 409
    finally:
        release.set()


def test_stop_sets_the_flag_the_engine_polls(client, monkeypatch):
    c, _ = client
    observed = {}

    def fake_pipeline(**kwargs):
        should_stop = kwargs["should_stop"]
        for _ in range(200):          # stand-in for the search/fetch loop
            if should_stop():
                observed["halted"] = True
                return []
            time.sleep(0.02)
        observed["halted"] = False
        return []

    monkeypatch.setattr(server, "run_pipeline", fake_pipeline)
    c.post("/api/search/start", json={"limit": 50})
    time.sleep(0.05)

    body = c.post("/api/search/stop").json()
    assert body["stop_requested"] is True
    assert body["state"] in ("stopping", "stopped")

    for _ in range(100):
        body = c.get("/api/search/status").json()
        if body["state"] == "stopped":
            break
        time.sleep(0.02)
    assert body["state"] == "stopped"
    assert observed["halted"] is True


def test_stop_with_no_run_is_a_no_op(client):
    c, _ = client
    body = c.post("/api/search/stop").json()
    assert body["state"] == "idle"


def test_pipeline_failure_is_reported_not_swallowed(client, monkeypatch):
    c, _ = client

    def boom(**kwargs):
        raise RuntimeError("search backend exploded")

    monkeypatch.setattr(server, "run_pipeline", boom)
    c.post("/api/search/start", json={"limit": 1})
    for _ in range(100):
        body = c.get("/api/search/status").json()
        if body["state"] == "error":
            break
        time.sleep(0.02)
    assert body["state"] == "error"
    assert "exploded" in body["message"]
    # and the worker is reusable afterwards
    monkeypatch.setattr(server, "run_pipeline", lambda **kw: [])
    assert c.post("/api/search/start", json={"limit": 1}).status_code == 200


# --------------------------------------------------------------------------
# /api/posts
# --------------------------------------------------------------------------

def test_posts_defaults_to_relevant_only(client):
    c, db = client
    _store(db, 1, relevant=True)
    _store(db, 2, relevant=False)
    body = c.get("/api/posts").json()
    assert body["total"] == 1
    assert body["items"][0]["is_relevant"] is True


def test_posts_pagination(client):
    c, db = client
    for i in range(5):
        _store(db, i)
    page = c.get("/api/posts?limit=2&offset=0").json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert page["limit"] == 2 and page["offset"] == 0

    tail = c.get("/api/posts?limit=2&offset=4").json()
    assert len(tail["items"]) == 1
    assert tail["items"][0]["url"] != page["items"][0]["url"]


def test_posts_filters(client):
    c, db = client
    _store(db, 1, source=Source.STIPS, confidence=0.2)
    _store(db, 2, source=Source.FXP, confidence=0.95)
    assert c.get("/api/posts?source=fxp").json()["total"] == 1
    assert c.get("/api/posts?min_confidence=0.5").json()["total"] == 1
    assert c.get("/api/posts?search=מספר 1").json()["total"] == 1


def test_posts_rejects_out_of_range_params(client):
    c, _ = client
    assert c.get("/api/posts?limit=0").status_code == 422
    assert c.get("/api/posts?min_confidence=2").status_code == 422


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

def test_export_is_utf8_bom_csv_with_readable_hebrew(client):
    c, db = client
    _store(db, 1, quotes=["בלי שההורים ידעו", "אני בת 16"])
    r = c.get("/api/posts/export")

    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment; filename=" in r.headers["content-disposition"]
    # Excel needs the BOM to read the Hebrew columns as UTF-8.
    assert r.content.startswith(b"\xef\xbb\xbf")

    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["source"] == "stips"
    assert "סודיות רפואית" in rows[0]["title"]
    # Multi-valued fields are flattened for spreadsheet use.
    assert rows[0]["key_quotes"] == "בלי שההורים ידעו | אני בת 16"
    assert set(rows[0]) == set(server.CSV_COLUMNS)


def test_export_honours_filters(client):
    c, db = client
    _store(db, 1, relevant=True)
    _store(db, 2, relevant=False)

    relevant = list(csv.DictReader(io.StringIO(
        c.get("/api/posts/export").content.decode("utf-8-sig"))))
    everything = list(csv.DictReader(io.StringIO(
        c.get("/api/posts/export?relevant_only=false").content.decode("utf-8-sig"))))
    assert len(relevant) == 1 and len(everything) == 2


def test_stats_endpoint(client):
    c, db = client
    _store(db, 1, relevant=True)
    _store(db, 2, relevant=False)
    body = c.get("/api/stats").json()
    assert body == {"total": 2, "analyzed": 2, "relevant": 1}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_health_needs_no_token(client):
    """Health stays open so a tunnel/uptime check works without the secret."""
    c, _ = client
    r = c.get("/api/health", headers={"Authorization": ""})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/api/search/status", "/api/posts",
                                  "/api/posts/export", "/api/stats"])
def test_get_endpoints_reject_missing_token(client, path):
    c, _ = client
    assert c.get(path, headers={"Authorization": ""}).status_code == 401


@pytest.mark.parametrize("path", ["/api/search/start", "/api/search/stop"])
def test_post_endpoints_reject_missing_token(client, path):
    c, _ = client
    assert c.post(path, json={}, headers={"Authorization": ""}).status_code == 401


def test_wrong_token_rejected(client):
    c, _ = client
    r = c.get("/api/stats", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_bare_token_without_bearer_prefix_accepted(client):
    """Tolerate a pasted raw token so a mistyped header is not a silent 401."""
    c, _ = client
    assert c.get("/api/stats", headers={"Authorization": TEST_TOKEN}).status_code == 200
