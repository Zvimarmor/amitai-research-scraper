"""Live LLM smoke test. Skipped automatically when no API key is configured.

Run it on its own once a key is in `.env`:

    .venv/bin/python -m pytest tests/test_llm_live.py -v -s

It makes a handful of real API calls (cents at most) and checks the three things
that actually break in production: structured JSON parsing, correct verdicts on
a known-positive and a known-negative post, and recovery from a transient error.
"""
from __future__ import annotations

import pytest

from src import config
from src.models import LLMAnalysisResult, Post, Source
from src.screener import LLMScreener, expand_query

HAS_KEY = bool(config.GEMINI_API_KEY or config.OPENAI_API_KEY)
pytestmark = pytest.mark.skipif(
    not HAS_KEY, reason="No GEMINI_API_KEY / OPENAI_API_KEY configured"
)

# A real Stips thread, shortened: on-topic for the study.
POSITIVE = Post(
    url="https://stips.co.il/ask/19440091/x",
    source=Source.STIPS,
    title="איך אני יכול לקבל טיפול נפשי בלי שההורים שלי ידעו למה אני צריך בכלל",
    body="אני בן 15 ואני רוצה ללכת לפסיכולוג אבל אני לא רוצה שההורים שלי ידעו. "
         "יש סודיות רפואית גם לקטין או שהמטפל חייב לספר להם הכל?",
    comments=[
        "אם אתה קטין אין אפשרות בלי הסכמת הורים לרוב המקרים.",
        "יש חיסיון, אבל אם יש סכנה ממשית הפסיכיאטר חייב ליידע אפוטרופוס.",
        "תדבר עם היועצת בבית הספר, היא יכולה להפנות אותך בלי ההורים.",
    ],
)

# Mentions a medical term and parents, but is not about privacy or autonomy.
NEGATIVE = Post(
    url="https://stips.co.il/ask/000000/y",
    source=Source.STIPS,
    title="מתכון לעוגת גזר של אמא",
    body="אמא שלי הכינה עוגת גזר מעולה בשבת. היא אמרה שהסוכר בה מופחת כי אבא "
         "שלי בדיאטה. מישהו יודע איך מכינים את הזיגוג הלבן שיוצא כזה חלק?",
    comments=["גבינת שמנת, חמאה ואבקת סוכר. בהצלחה!"],
)


@pytest.fixture(scope="module")
def screener() -> LLMScreener:
    return LLMScreener()


def test_provider_connects(screener):
    print(f"\nProvider: {screener.provider} / model: {screener.model}")
    assert screener.provider in ("gemini", "openai")


def test_structured_output_parses_and_validates(screener):
    result = screener.analyze(POSITIVE)
    print("\n" + result.model_dump_json(indent=2))
    print("tokens:", screener.last_usage)

    assert isinstance(result, LLMAnalysisResult)
    assert 0.0 <= result.confidence <= 1.0
    assert result.topic in ("t1d", "psychiatric", "both", "other")
    assert isinstance(result.key_quotes, list)
    assert result.summary.strip()
    # Token accounting is populated, so cost per run can be tracked.
    assert screener.last_usage.get("total", 0) > 0


def test_known_positive_is_judged_relevant(screener):
    result = screener.analyze(POSITIVE)
    assert result.is_relevant is True
    assert result.topic in ("psychiatric", "both")
    assert result.privacy_tension is True
    assert result.confidence >= 0.5
    # Quotes must be verbatim, since they are used as qualitative evidence.
    for quote in result.key_quotes:
        assert quote.strip(" .…"), "empty quote returned"


def test_known_negative_is_rejected(screener):
    result = screener.analyze(NEGATIVE)
    print(f"\nnegative verdict: relevant={result.is_relevant} "
          f"conf={result.confidence} topic={result.topic}")
    assert result.is_relevant is False


def test_retries_recover_from_a_transient_failure(screener, monkeypatch):
    """First call raises, second succeeds: analyze() must return, not propagate."""
    calls = {"n": 0}
    real = screener.complete_json

    def flaky(prompt, system, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated 503 from provider")
        return real(prompt, system, schema)

    monkeypatch.setattr(screener, "complete_json", flaky)
    result = screener.analyze(POSITIVE)
    assert calls["n"] == 2
    assert isinstance(result, LLMAnalysisResult)


def test_malformed_output_eventually_raises(screener, monkeypatch):
    """After exhausting retries the error surfaces, rather than silently passing."""
    monkeypatch.setattr(
        screener, "complete_json",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("not json")))
    with pytest.raises(RuntimeError, match="LLM screening failed"):
        screener.analyze(POSITIVE, retries=1)


def test_query_expansion_returns_usable_terms():
    terms = expand_query("teens hiding insulin use from their parents")
    print("\nexpanded terms:", terms)
    assert terms, "expansion returned nothing"
    assert all(isinstance(t, str) and t.strip() for t in terms)
    assert len(terms) == len(set(t.lower() for t in terms)), "duplicate terms"
