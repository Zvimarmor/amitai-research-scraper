"""Offline smoke tests - no network, no API keys. Run: python -m tests.test_smoke"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.db import Database
from src.fetcher import (
    _is_reddit_interstitial, parse_fxp, parse_reddit_html, parse_reddit_json, parse_stips,
)
from src.models import LLMAnalysisResult, Post, Source
from src.screener import regex_screen
from src.searcher import build_queries

FXP_HTML = """
<html><head><title>ignored</title></head><body>
<h1 class="p-title-value">סוכרת נעורים - ההורים שלי לא מפסיקים להתערב</h1>
<div class="bbWrapper">אני בת 16 עם סוכרת סוג 1 כבר ארבע שנים. ההורים שלי רואים את כל
הנתונים מהחיישן בטלפון שלהם ואני מרגישה שאין לי שום פרטיות. רציתי לשאול את
האנדוקרינולוג משהו בלי ידיעת ההורים אבל הוא אמר שהוא חייב לספר להורים.</div>
<div class="bbWrapper">גם אני עברתי את זה. בגיל 16 מותר לך לבקש שיחה לבד עם הרופא,
יש חיסיון על מידע רפואי.</div>
</body></html>
"""

STIPS_HTML = """
<html><body><h1>אפשר ללכת לפסיכולוג בלי שההורים ידעו?</h1>
<div class="question-text">אני בן 15 ואני רוצה טיפול נפשי אבל אני מפחד שהפסיכולוג
יספר להורים שלי. יש סודיות רפואית גם לקטין?</div>
<div class="answer-text">כן, יש חיסיון. אבל אם יש סכנה ממשית הפסיכיאטר חייב ליידע
אפוטרופוס. תלוי בגיל ובהסכמה.</div>
<div class="answer-text">תדבר עם היועצת בבית ספר, היא יכולה לעזור בלי ההורים.</div>
</body></html>
"""

REDDIT_JSON = [
    {"data": {"children": [{"data": {
        "title": "16f, therapist told my parents about my diagnosis",
        "selftext": "I started therapy for anxiety and depression. I thought it was "
                    "confidential but my therapist told my parents. Do minors have any "
                    "medical privacy at all?",
        "author": "throwaway_x"}}]}},
    {"data": {"children": [
        {"data": {"body": "Confidentiality for a minor depends on jurisdiction and risk."}},
        {"data": {"body": "[deleted]"}},
    ]}},
]


OLD_REDDIT_HTML = """
<html><body>
<div class="top-matter"><a class="title">Therapist told my parents - 16m</a></div>
<div class="expando"><div class="usertext-body"><div class="md">
I am 16 and in therapy for depression. My therapist told my parents about my
diagnosis and medication without asking me. Is there any confidentiality for a minor?
</div></div></div>
<div class="commentarea">
  <div class="comment"><div class="entry"><div class="usertext-body"><div class="md">
  Confidentiality for minors is limited when there is a safety risk.</div></div></div></div>
  <div class="comment"><div class="entry"><div class="usertext-body"><div class="md">
  [deleted]</div></div></div></div>
</div></body></html>
"""

BLOCKED_HTML = "<html><head><title>Welcome to Reddit</title></head><body>x</body></html>"


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def main() -> None:
    # --- parsers -------------------------------------------------------
    fxp = parse_fxp(FXP_HTML, "https://www.fxp.co.il/showthread.php?t=1", "q")
    check("fxp title parsed", "סוכרת נעורים" in fxp.title)
    check("fxp body + 1 comment", fxp.body and len(fxp.comments) == 1)

    stips = parse_stips(STIPS_HTML, "https://www.stips.co.il/ask/1", "q")
    check("stips question parsed", "פסיכולוג" in stips.body or "טיפול נפשי" in stips.body)
    check("stips 2 answers", len(stips.comments) == 2)

    rdt = parse_reddit_json(REDDIT_JSON, "https://www.reddit.com/r/x/comments/y/", "q")
    check("reddit source", rdt.source is Source.REDDIT)
    check("reddit drops [deleted]", len(rdt.comments) == 1)

    rhtml = parse_reddit_html(OLD_REDDIT_HTML, "https://www.reddit.com/r/x/comments/z/", "q")
    check("old.reddit title parsed", "Therapist told my parents" in rhtml.title)
    check("old.reddit selftext parsed", "confidentiality" in rhtml.body.lower())
    check("old.reddit drops [deleted]", len(rhtml.comments) == 1)

    blocked = parse_reddit_html(BLOCKED_HTML, "https://www.reddit.com/r/x/comments/z/", "q")
    check("interstitial detected", _is_reddit_interstitial(blocked))
    check("real thread not flagged as interstitial", not _is_reddit_interstitial(rhtml))

    # --- regex screen --------------------------------------------------
    for post in (fxp, stips, rdt, rhtml):
        r = regex_screen(post, require_adolescent=True)
        check(f"regex passes {post.source.value}: {r.reason}", r.passed)

    noise = Post(url="https://x.co/1", source=Source.OTHER,
                 title="Recipe", body="Pasta with tomatoes. " * 20)
    check("regex rejects noise", not regex_screen(noise).passed)

    # --- db ------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.sqlite3")
        analysis = LLMAnalysisResult(is_relevant=True, topic="t1d", privacy_tension=True,
                                     confidence=1.7, summary="s", key_quotes="one quote")
        check("confidence clamped", analysis.confidence == 1.0)
        check("key_quotes coerced", analysis.key_quotes == ["one quote"])

        db.upsert_post(fxp, regex=regex_screen(fxp), analysis=analysis)
        check("url dedup", db.seen_url(fxp.url))
        check("hash dedup", db.seen_hash(fxp.content_hash))
        db.upsert_post(fxp, regex=regex_screen(fxp), analysis=analysis)
        check("upsert does not duplicate", db.stats()["total"] == 1)
        check("relevant counted", db.stats()["relevant"] == 1)
        rows = db.fetch_all(relevant_only=True)
        check("analysis_json round-trips", rows[0]["analysis_json"]["topic"] == "t1d")

    # --- queries -------------------------------------------------------
    qs = build_queries(["t1d"], sites=["fxp.co.il", "reddit.com"])
    check("dorks are site-scoped", all(q.query.startswith("site:") for q in qs))
    check("reddit dorks use english", any("teen" in q.query for q in qs if q.site == "reddit.com"))

    print(f"\nAll smoke tests passed. ({len(qs)} sample queries generated)")


if __name__ == "__main__":
    main()


def test_smoke_suite():
    """So `pytest tests/` runs these fixture checks alongside the unit tests."""
    main()
