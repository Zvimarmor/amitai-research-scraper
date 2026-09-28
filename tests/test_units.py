"""Unit tests: Hebrew lexicon matching, clitic handling, and DB deduplication."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.db import Database
from src.models import LLMAnalysisResult, Post, RegexScreenResult, Source
from src.screener import (
    _ADOLESCENT, _MEDICAL, _PRIVACY, _backoff_for, _compile, _extract_json, _matches,
    regex_screen,
)


# --------------------------------------------------------------------------
# Hebrew matching
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,term", [
    ("אני נער בן 16", "נער"),
    ("קבוצת נערים בתיכון", "נערים"),
    ("היא נערה בת 15", "נערה"),
    ("קטין לא יכול לחתום", "קטין"),
])
def test_adolescent_terms_match(text, term):
    assert term in _matches(text, _ADOLESCENT)


@pytest.mark.parametrize("text", [
    "נערך לאחרונה על ידי UpYours",   # "was edited" - the vBulletin post footer
    "נערכה בדיקה במעבדה",            # "an examination was conducted"
])
def test_short_term_does_not_match_inside_longer_word(text):
    """The bug this guards: "נער" (youth) matching inside "נערך" (was edited),
    which appears on every vBulletin post and made every FXP thread a hit."""
    assert "נער" not in _matches(text, _ADOLESCENT)


@pytest.mark.parametrize("text,expected", [
    ("סיפרתי להורים", "הורים"),        # ל prefix
    ("ושהאמא ידעה", "אמא"),            # ושה - three clitics
    ("המידע הרפואי שלי", "מידע רפואי"),
    ("בלי ידיעת ההורים", "בלי ידיעת"),
])
def test_clitic_prefixes_still_match(text, expected):
    assert expected in _matches(text, _PRIVACY)


def test_latin_terms_are_word_bounded_and_case_insensitive():
    assert "therapy" in _matches("I started Therapy last year", _MEDICAL)
    # "therapy" must not match inside "therapist"
    assert "therapy" not in _matches("my therapist said", _MEDICAL)


def test_multiword_hebrew_phrase_matches_as_substring():
    assert "טיפול נפשי" in _matches("קיבלתי טיפול נפשי טוב", _MEDICAL)


def test_compile_handles_regex_metacharacters():
    """Terms are escaped, so a dot or plus in a term is literal, not a wildcard."""
    patterns = _compile(["c-peptide", "vitamin d3"])
    assert patterns[0][1].search("c-peptide test")
    assert not patterns[0][1].search("cXpeptide test")


# --------------------------------------------------------------------------
# regex_screen co-occurrence rules
# --------------------------------------------------------------------------

def _post(body: str, title: str = "t") -> Post:
    return Post(url="https://x.co/1", source=Source.STIPS, title=title, body=body)


def test_requires_both_medical_and_privacy():
    medical_only = _post("יש לי סוכרת סוג 1 " * 10)
    assert not regex_screen(medical_only).passed
    assert "no parental/privacy terms" in regex_screen(medical_only).reason

    privacy_only = _post("ההורים שלי לא נותנים לי פרטיות " * 10)
    assert not regex_screen(privacy_only).passed
    assert "no medical terms" in regex_screen(privacy_only).reason


def test_co_occurrence_passes():
    post = _post("יש לי סוכרת ואני לא רוצה שההורים שלי ידעו על רמות הסוכר " * 4)
    assert regex_screen(post).passed


def test_require_adolescent_flag():
    body = "יש לי סוכרת ואני מסתיר את זה מההורים שלי לגמרי " * 4
    assert regex_screen(_post(body)).passed
    result = regex_screen(_post(body), require_adolescent=True)
    assert not result.passed and "adolescent" in result.reason

    with_age = _post(body + " אני בת 16")
    assert regex_screen(with_age, require_adolescent=True).passed


def test_too_short_is_rejected_before_matching():
    result = regex_screen(_post("סוכרת הורים"))
    assert not result.passed and "Too little text" in result.reason


def test_comments_count_toward_co_occurrence():
    """The medical term is in the question and the privacy term only in a reply."""
    post = Post(
        url="https://x.co/2", source=Source.STIPS,
        title="שאלה על סוכרת",
        body="אובחנתי עם סוכרת נעורים לפני שנה, מה עושים עכשיו בבית הספר?",
        comments=["כדאי שתדבר עם ההורים שלך לפני שאתה מחליט משהו על הטיפול"],
    )
    assert regex_screen(post).passed


# --------------------------------------------------------------------------
# LLM output parsing / recovery
# --------------------------------------------------------------------------

def test_extract_json_handles_fenced_and_prefixed_output():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Here you go:\n{"a": 2}') == {"a": 2}
    assert _extract_json('{"a": 3}') == {"a": 3}


def test_analysis_result_coerces_bad_values():
    r = LLMAnalysisResult.model_validate({
        "is_relevant": True, "topic": "t1d", "privacy_tension": True,
        "confidence": "1.9", "summary": "s", "key_quotes": "single string",
    })
    assert r.confidence == 1.0
    assert r.key_quotes == ["single string"]
    assert LLMAnalysisResult(is_relevant=False, confidence="junk").confidence == 0.0


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------

@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield Database(Path(tmp) / "t.sqlite3")


def test_content_hash_ignores_whitespace_and_case():
    a = Post(url="https://x.co/a", source=Source.FXP, title="T", body="Hello   World")
    b = Post(url="https://x.co/b", source=Source.FXP, title="t", body="hello world")
    assert a.content_hash == b.content_hash


def test_content_hash_differs_on_real_content_change():
    a = Post(url="https://x.co/a", source=Source.FXP, title="T", body="Hello world")
    b = Post(url="https://x.co/a", source=Source.FXP, title="T", body="Hello worlds")
    assert a.content_hash != b.content_hash


def test_same_url_upserts_rather_than_duplicating(db):
    post = Post(url="https://x.co/a", source=Source.STIPS, title="T", body="body text here")
    db.upsert_post(post, regex=RegexScreenResult(passed=True))
    db.upsert_post(post, regex=RegexScreenResult(passed=True))
    assert db.stats()["total"] == 1


def test_upsert_adds_analysis_to_an_existing_row(db):
    post = Post(url="https://x.co/a", source=Source.STIPS, title="T", body="body text here")
    db.upsert_post(post, regex=RegexScreenResult(passed=True))
    assert db.stats()["analyzed"] == 0

    db.upsert_post(post, regex=RegexScreenResult(passed=True),
                   analysis=LLMAnalysisResult(is_relevant=True, topic="t1d", confidence=0.9))
    assert db.stats()["analyzed"] == 1 and db.stats()["relevant"] == 1


def test_upsert_without_analysis_does_not_erase_existing_analysis(db):
    """COALESCE in the upsert: re-scraping a post must not drop its verdict."""
    post = Post(url="https://x.co/a", source=Source.STIPS, title="T", body="body text here")
    db.upsert_post(post, analysis=LLMAnalysisResult(is_relevant=True, confidence=0.8))
    db.upsert_post(post)  # a later pass with no analysis
    assert db.stats()["analyzed"] == 1


def test_seen_hash_detects_same_content_at_a_different_url(db):
    a = Post(url="https://x.co/a", source=Source.FXP, title="T", body="identical body text")
    b = Post(url="https://x.co/b", source=Source.FXP, title="T", body="identical body text")
    db.upsert_post(a)
    assert db.seen_hash(b.content_hash)
    assert not db.seen_url(b.url)
    assert db.is_duplicate(b.url, b.content_hash)


# --------------------------------------------------------------------------
# Pagination and filtering
# --------------------------------------------------------------------------

def _seed(db, n=5):
    for i in range(n):
        db.upsert_post(
            Post(url=f"https://x.co/{i}", source=Source.STIPS if i % 2 else Source.FXP,
                 title=f"post {i}", body=f"body number {i} with enough text to store"),
            regex=RegexScreenResult(passed=True),
            analysis=LLMAnalysisResult(
                is_relevant=(i % 2 == 0), topic="t1d", confidence=i / 10),
        )


def test_pagination_reports_total_before_slicing(db):
    _seed(db, 5)
    page = db.fetch_posts(limit=2, offset=0, relevant_only=False)
    assert page["total"] == 5 and len(page["items"]) == 2

    page2 = db.fetch_posts(limit=2, offset=4, relevant_only=False)
    assert page2["total"] == 5 and len(page2["items"]) == 1


def test_relevant_only_and_min_confidence_filters(db):
    _seed(db, 5)
    assert db.fetch_posts(limit=50, relevant_only=True)["total"] == 3   # i = 0, 2, 4
    assert db.fetch_posts(limit=50, relevant_only=False,
                          min_confidence=0.3)["total"] == 2             # i = 3, 4


def test_source_and_text_filters(db):
    _seed(db, 5)
    assert db.fetch_posts(limit=50, relevant_only=False, source="stips")["total"] == 2
    assert db.fetch_posts(limit=50, relevant_only=False, search="number 3")["total"] == 1


# --------------------------------------------------------------------------
# Retry backoff
# --------------------------------------------------------------------------

@pytest.mark.parametrize("attempt,expected", [(0, 1.0), (1, 2.0), (2, 4.0)])
def test_generic_transient_errors_back_off_fast(attempt, expected):
    assert _backoff_for(Exception("connection reset by peer"), attempt) == expected


@pytest.mark.parametrize("attempt,expected", [(0, 10.0), (1, 20.0), (2, 40.0)])
def test_overload_backs_off_longer_than_a_generic_transient(attempt, expected):
    """A 503 capacity spike outlasts 1-2-4s and took down a live test run."""
    assert _backoff_for(Exception("503 UNAVAILABLE high demand"), attempt) == expected


def test_overload_backoff_is_capped():
    assert _backoff_for(Exception("503 model is overloaded"), 9) == 60.0


def test_rate_limit_wins_over_overload_when_both_appear():
    """429 must keep its quota-window wait even if the text mentions 503."""
    assert _backoff_for(Exception("429 quota exceeded; service unavailable"), 0) >= 30.0


@pytest.mark.parametrize("message", [
    "429 RESOURCE_EXHAUSTED quota exceeded",
    "Rate limit reached for this model",
    "too many requests",
])
def test_rate_limits_back_off_on_the_quota_timescale(message):
    """A 1-2-4s backoff burns every retry inside one per-minute quota window,
    which is how a whole screening run died after 16 posts."""
    assert _backoff_for(Exception(message), 0) >= 30.0


def test_provider_retry_delay_hint_is_honoured():
    exc = Exception("429 RESOURCE_EXHAUSTED {'retryDelay': '47s'}")
    assert _backoff_for(exc, 0) == 49.0        # hint + 2s of margin


def test_backoff_is_capped():
    assert _backoff_for(Exception("429 quota {'retryDelay': '600s'}"), 0) == 120.0
    assert _backoff_for(Exception("429 quota"), 9) == 120.0


def test_daily_quota_fails_fast_instead_of_waiting_out_retries():
    """A per-day cap cannot clear mid-run; waiting 30-90s per attempt wasted
    minutes per post before this was special-cased."""
    exc = Exception("429 RESOURCE_EXHAUSTED quota metric "
                    "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert _backoff_for(exc, 0) == 0.0


def test_per_minute_quota_still_waits():
    """Only the daily window fails fast - a per-minute cap must still back off."""
    exc = Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerMinutePerProject-FreeTier")
    assert _backoff_for(exc, 0) >= 30.0


# --------------------------------------------------------------------------
# CGM vocabulary precision
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "איזה בושם של ליברה Ysl יש לכן ואתן ממליצות",   # YSL Libre perfume
    "רשמתי ליברה שיצאה ממש צבועה",                  # "Libre" as a given name
    "קניתי משאבה חדשה לאמא שלי",                    # any pump, not insulin
])
def test_cgm_brand_names_do_not_match_unrelated_threads(text):
    """Bare "ליברה"/"משאבה" pulled perfume and breast-pump threads into the
    corpus, so only the qualified CGM forms are lexicon terms."""
    assert not _matches(text, _MEDICAL)


@pytest.mark.parametrize("text,term", [
    ("שמתי חיישן ליברה 2 חדש", "חיישן ליברה"),
    ("הדקסקום מחובר לאפליקציה", "דקסקום"),
    ("משאבת אינסולין וסנסור", "משאבת אינסולין"),
    ("my parents watch my dexcom", "dexcom"),
])
def test_real_cgm_terms_still_match(text, term):
    assert term in _matches(text, _MEDICAL)


@pytest.mark.parametrize("text,term", [
    ("ההורים עוקבים אחרי הסוכר שלי", "עוקבים"),
    ("הם בודקים לי את האפליקציה", "בודקים לי"),
    ("מקבלים התראות להורים על כל ירידה", "התראות להורים"),
    ("יש מעקב מרחוק על הנתונים", "מעקב"),
])
def test_remote_monitoring_terms_are_privacy_terms(text, term):
    """Continuous CGM monitoring is a different privacy shape from one-off
    disclosure, so the monitoring vocabulary has to register as privacy."""
    assert term in _matches(text, _PRIVACY)


@pytest.mark.parametrize("text", [
    "החיישן מצפצף כל הלילה וההורים רואים לי את הסוכר בטלפון שלהם ואני רק רוצה לכבות את ההתראות",
    "הדקסקום מחובר לאפליקציה שלהם, הם חופרים לי על כל צפצוף, ניתקתי את שיתוף נתונים",
    "שמתי סנסור והם מציקים לי כל הזמן, יש קוד לאפליקציה שאני יכול לשנות?",
])
def test_friction_vernacular_passes_without_diagnostic_phrasing(text):
    """Adolescents describe monitoring friction ("it beeps", "they nag me"), not
    "parental involvement in disease management" - the gate must accept a device
    term plus friction language with no formal diagnosis wording present."""
    post = Post(url="https://stips.co.il/ask/f", source=Source.STIPS, title="t", body=text * 2)
    assert regex_screen(post).passed


@pytest.mark.parametrize("term", [
    "צפצופים", "התראות בלילה", "לכבות", "שיתוף נתונים",
    "קוד לאפליקציה", "חופרים", "מציקים", "עוקבים אחרי",
])
def test_friction_terms_are_registered_privacy_terms(term):
    assert term in [t for t, _ in _PRIVACY]
