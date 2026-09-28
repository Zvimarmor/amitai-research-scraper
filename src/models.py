"""Pydantic data models for the research pipeline."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Source(str, Enum):
    FXP = "fxp"
    STIPS = "stips"
    REDDIT = "reddit"
    OTHER = "other"

    @classmethod
    def from_url(cls, url: str) -> "Source":
        low = url.lower()
        if "fxp.co.il" in low:
            return cls.FXP
        if "stips.co.il" in low:
            return cls.STIPS
        if "reddit.com" in low:
            return cls.REDDIT
        return cls.OTHER


class SearchHit(BaseModel):
    """A raw result returned by the search engine, before fetching."""

    url: str
    title: str = ""
    snippet: str = ""
    source: Source = Source.OTHER
    query: str = ""


class ScrapingQuery(BaseModel):
    """A single generated search query (dork) plus its provenance."""

    query: str
    site: Optional[str] = None
    theme: str = ""
    language: str = "he"
    max_results: int = Field(default=10, ge=1, le=100)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.query


class Post(BaseModel):
    """A fetched and parsed discussion item."""

    url: str
    source: Source
    title: str = ""
    body: str = ""
    comments: list[str] = Field(default_factory=list)
    author: Optional[str] = None
    posted_at: Optional[str] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    query: str = ""

    @property
    def full_text(self) -> str:
        parts = [self.title, self.body, *self.comments]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    @property
    def content_hash(self) -> str:
        normalized = " ".join(self.full_text.split()).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def truncated(self, limit: int = 12000) -> str:
        text = self.full_text
        return text if len(text) <= limit else text[:limit] + "\n[...truncated...]"


class RegexScreenResult(BaseModel):
    """Output of the cheap first-pass filter."""

    passed: bool
    medical_terms: list[str] = Field(default_factory=list)
    privacy_terms: list[str] = Field(default_factory=list)
    adolescent_terms: list[str] = Field(default_factory=list)
    reason: str = ""


class LLMAnalysisResult(BaseModel):
    """Structured verdict from the second-pass LLM classifier."""

    is_relevant: bool
    topic: str = ""
    privacy_tension: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    key_quotes: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return min(max(f, 0.0), 1.0)

    @field_validator("key_quotes", mode="before")
    @classmethod
    def _listify(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)


class ScreenedPost(BaseModel):
    """A post joined with both screening stages, ready for storage / display."""

    post: Post
    regex: RegexScreenResult
    analysis: Optional[LLMAnalysisResult] = None
    error: Optional[str] = None
