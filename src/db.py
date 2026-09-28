"""SQLite persistence with URL + content-hash deduplication.

Raw sqlite3 is used deliberately: the schema is tiny and an ORM would add a
dependency without buying anything here.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .config import DB_PATH
from .models import LLMAnalysisResult, Post, RegexScreenResult

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    content_hash  TEXT NOT NULL,
    source        TEXT NOT NULL,
    title         TEXT,
    raw_text      TEXT NOT NULL,
    query         TEXT,
    regex_json    TEXT,
    analysis_json TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_hash   ON posts (content_hash);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts (source);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around a SQLite file. Safe to construct repeatedly."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # -- dedup helpers -------------------------------------------------

    def seen_url(self, url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM posts WHERE url = ?", (url,)).fetchone()
        return row is not None

    def seen_hash(self, content_hash: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM posts WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return row is not None

    def is_duplicate(self, url: str, content_hash: str) -> bool:
        return self.seen_url(url) or self.seen_hash(content_hash)

    # -- writes --------------------------------------------------------

    def upsert_post(
        self,
        post: Post,
        regex: Optional[RegexScreenResult] = None,
        analysis: Optional[LLMAnalysisResult] = None,
    ) -> int:
        """Insert a post, or update its analysis if the URL is already stored.

        Returns the row id.
        """
        now = _now()
        regex_json = regex.model_dump_json() if regex else None
        analysis_json = analysis.model_dump_json() if analysis else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO posts (url, content_hash, source, title, raw_text, query,
                                   regex_json, analysis_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    content_hash  = excluded.content_hash,
                    title         = excluded.title,
                    raw_text      = excluded.raw_text,
                    regex_json    = COALESCE(excluded.regex_json, posts.regex_json),
                    analysis_json = COALESCE(excluded.analysis_json, posts.analysis_json),
                    updated_at    = excluded.updated_at
                """,
                (
                    post.url,
                    post.content_hash,
                    post.source.value,
                    post.title,
                    post.full_text,
                    post.query,
                    regex_json,
                    analysis_json,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT id FROM posts WHERE url = ?", (post.url,)).fetchone()
        return int(row["id"])

    # -- reads ---------------------------------------------------------

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("regex_json", "analysis_json"):
            if item.get(key):
                try:
                    item[key] = json.loads(item[key])
                except json.JSONDecodeError:
                    log.warning("Malformed %s for row %s", key, item.get("id"))
                    item[key] = None
        return item

    def fetch_all(self, limit: int = 100, relevant_only: bool = False) -> list[dict]:
        return self.fetch_posts(limit=limit, relevant_only=relevant_only)["items"]

    def fetch_posts(
        self,
        limit: int = 50,
        offset: int = 0,
        relevant_only: bool = False,
        source: Optional[str] = None,
        min_confidence: float = 0.0,
        search: Optional[str] = None,
        analyzed_only: bool = False,
    ) -> dict:
        """Paginated, filtered read. Returns {"items": [...], "total": n}.

        `is_relevant` and `confidence` live inside the analysis JSON blob rather
        than in columns, so those two filters are applied in Python after the
        SQL-level filters have cut the set down.
        """
        where, params = [], []
        if source:
            where.append("source = ?")
            params.append(source)
        if search:
            where.append("(title LIKE ? OR raw_text LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if analyzed_only or relevant_only or min_confidence > 0:
            where.append("analysis_json IS NOT NULL")
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM posts {clause} ORDER BY id DESC", params
            ).fetchall()

        items = []
        for row in rows:
            item = self._decode(row)
            analysis = item.get("analysis_json") or {}
            if relevant_only and not analysis.get("is_relevant"):
                continue
            if min_confidence and float(analysis.get("confidence") or 0) < min_confidence:
                continue
            items.append(item)

        total = len(items)
        if offset:
            items = items[offset:]
        if limit:
            items = items[:limit]
        return {"items": items, "total": total}

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
            analyzed = conn.execute(
                "SELECT COUNT(*) c FROM posts WHERE analysis_json IS NOT NULL"
            ).fetchone()["c"]
            relevant = conn.execute(
                "SELECT COUNT(*) c FROM posts WHERE analysis_json LIKE '%\"is_relevant\":true%'"
            ).fetchone()["c"]
        return {"total": total, "analyzed": analyzed, "relevant": relevant}
