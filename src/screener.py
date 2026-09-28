"""Two-pass screening: cheap regex co-occurrence filter, then an LLM classifier."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from . import config
from .models import LLMAnalysisResult, Post, RegexScreenResult

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Pass 1: lexicons
# --------------------------------------------------------------------------

MEDICAL_TERMS = [
    # Type 1 diabetes (Hebrew)
    "סוכרת", "סכרת", "אינסולין", "משאבת אינסולין", "חיישן", "רמות סוכר",
    "היפוגליקמיה", "היפרגליקמיה", "אנדוקרינולוג", "סוכרת נעורים",
    # Psychiatric / mental health (Hebrew)
    "פסיכיאטר", "פסיכולוג", "טיפול נפשי", "פסיכותרפיה", "אשפוז", "דיכאון",
    "חרדה", "ריטלין", "קונצרטה", "נוגדי דיכאון", "אבחון", "הפרעת קשב",
    "מרפאה", "רופא", "קופת חולים", "תיק רפואי", "מרשם",
    # English
    "diabetes", "insulin", "t1d", "glucose", "endocrinologist",
    "psychiatrist", "therapist", "therapy", "antidepressant", "adhd",
    "medication", "diagnosis", "clinic", "medical record", "prescription",
]

PRIVACY_TERMS = [
    # Hebrew
    "הורים", "אמא", "אבא", "ההורים שלי", "סודיות", "פרטיות", "סוד",
    "בלי ידיעת", "בלי שההורים", "לא סיפרתי", "מסתיר", "להסתיר", "יגלו",
    "הסכמה", "חיסיון", "מידע רפואי", "לספר להורים", "אפוטרופוס",
    # English
    "parents", "mom", "dad", "confidential", "confidentiality", "privacy",
    "secret", "without my parents", "hide", "hiding", "told my parents",
    "consent", "guardian",
]

ADOLESCENT_TERMS = [
    "קטין", "קטינה", "נער", "נערה", "נערים", "נערות", "מתבגר", "מתבגרת", "בת 14", "בן 14", "בת 15",
    "בן 15", "בת 16", "בן 16", "בת 17", "בן 17", "תיכון", "כיתה י",
    "teen", "teenager", "adolescent", "minor", "high school",
    "14f", "15f", "16f", "17f", "14m", "15m", "16m", "17m",
]


HEB = r"\u0590-\u05FF"
# One- or two-letter clitics that attach to the front of a Hebrew word
# (ו/ה/ב/ל/מ/ש/כ, up to three), e.g. "להורים", "ושהפסיכיאטר".
HEB_PREFIX = r"[\u05d5\u05d4\u05d1\u05dc\u05de\u05e9\u05db]{0,3}"


def _compile(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    """Build one pattern per term.

    Latin terms get plain word boundaries. Hebrew has no case and `\b` is
    unreliable against it, so:

    * short single words are anchored between non-Hebrew characters (allowing a
      leading clitic) - otherwise "נער" (youth) matches inside "נערך" (was
      edited), the footer on every vBulletin post;
    * multi-word phrases allow a clitic on each word, so "מידע רפואי" matches
      "המידע הרפואי";
    * long single words match as substrings, which keeps prefixes and suffixes
      working without enumerating them.
    """
    out = []
    for term in terms:
        if re.search(r"[A-Za-z]", term):
            pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        elif " " in term:
            # Every word of a Hebrew phrase can take its own clitic, so
            # "מידע רפואי" has to match "המידע הרפואי". No trailing boundary,
            # which keeps suffixed forms of the final word matching.
            words = r"\s+".join(
                f"{HEB_PREFIX}{re.escape(w)}" for w in term.split()
            )
            pattern = re.compile(f"(?<![{HEB}]){words}")
        elif len(term) <= 4:
            pattern = re.compile(
                f"(?<![{HEB}]){HEB_PREFIX}{re.escape(term)}(?![{HEB}])"
            )
        else:
            # Long single words match as substrings, so Hebrew prefixes and
            # suffixes ("המידע", "סוכרתי") are covered without enumeration.
            pattern = re.compile(re.escape(term))
        out.append((term, pattern))
    return out


_MEDICAL = _compile(MEDICAL_TERMS)
_PRIVACY = _compile(PRIVACY_TERMS)
_ADOLESCENT = _compile(ADOLESCENT_TERMS)


def _matches(text: str, compiled: list[tuple[str, re.Pattern]]) -> list[str]:
    return [term for term, pattern in compiled if pattern.search(text)]


def regex_screen(post: Post, require_adolescent: bool = False) -> RegexScreenResult:
    """Require co-occurrence of >=1 medical term and >=1 parental/privacy term."""
    text = post.full_text
    if len(text.strip()) < 80:
        return RegexScreenResult(passed=False, reason="Too little text extracted")

    medical = _matches(text, _MEDICAL)
    privacy = _matches(text, _PRIVACY)
    adolescent = _matches(text, _ADOLESCENT)

    reasons = []
    if not medical:
        reasons.append("no medical terms")
    if not privacy:
        reasons.append("no parental/privacy terms")
    if require_adolescent and not adolescent:
        reasons.append("no adolescent markers")

    return RegexScreenResult(
        passed=not reasons,
        medical_terms=medical[:15],
        privacy_terms=privacy[:15],
        adolescent_terms=adolescent[:15],
        reason="; ".join(reasons) if reasons else "co-occurrence satisfied",
    )


# --------------------------------------------------------------------------
# Pass 2: LLM classifier
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a research assistant screening public forum posts for an \
academic study on adolescent medical privacy in Israel. The study examines the tension \
between adolescent autonomy and parental involvement in two clinical contexts: \
Type 1 Diabetes and psychiatric / mental-health care.

Judge ONLY what the text actually says. Do not speculate about the author.

A post is RELEVANT when it is written by, or is about, an adolescent (roughly 12-18) AND \
it substantively touches adolescent medical confidentiality, medical decision-making \
autonomy, or conflict/negotiation with parents over medical information or treatment.

Posts that merely mention an illness, or are news/marketing/professional content with no \
personal account of the privacy or autonomy question, are NOT relevant.

Return strict JSON matching the schema:
- is_relevant: boolean
- topic: one of "t1d", "psychiatric", "both", "other"
- privacy_tension: true only if the text shows an actual conflict or concern about \
parents knowing, consenting to, or controlling medical information or care
- confidence: 0.0-1.0
- summary: 1-2 sentences, English
- key_quotes: up to 3 short verbatim quotes from the text, in the original language
"""

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "is_relevant": {"type": "boolean"},
        "topic": {"type": "string", "enum": ["t1d", "psychiatric", "both", "other"]},
        "privacy_tension": {"type": "boolean"},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "key_quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "is_relevant", "topic", "privacy_tension", "confidence", "summary", "key_quotes",
    ],
    "additionalProperties": False,
}


def _build_user_prompt(post: Post) -> str:
    return (
        f"SOURCE: {post.source.value}\n"
        f"URL: {post.url}\n"
        f"TITLE: {post.title}\n\n"
        f"CONTENT:\n{post.truncated()}\n"
    )


class LLMScreener:
    """Provider-agnostic structured classifier (Gemini or OpenAI)."""

    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None,
                 delay: float = 1.0):
        self.provider = (provider or config.LLM_PROVIDER).lower()
        self.delay = delay
        # Token counts from the most recent call, for cost tracking.
        self.last_usage: dict[str, int] = {}
        self.total_usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}
        if self.provider == "gemini":
            self.model = model or config.GEMINI_MODEL
            self._client = self._init_gemini()
        elif self.provider == "openai":
            self.model = model or config.OPENAI_MODEL
            self._client = self._init_openai()
        else:
            raise ValueError(f"Unknown LLM_PROVIDER {self.provider!r}; use gemini or openai")

    # -- provider setup -------------------------------------------------

    def _init_gemini(self):
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("pip install google-genai") from exc
        return genai.Client(api_key=config.GEMINI_API_KEY)

    def _init_openai(self):
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set (see .env.example)")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("pip install openai") from exc
        return OpenAI(api_key=config.OPENAI_API_KEY)

    # -- calls -----------------------------------------------------------

    @staticmethod
    def _gemini_schema(schema: dict) -> dict:
        """Strip keys Gemini's response_schema rejects.

        OpenAI strict mode *requires* `additionalProperties: false`; Gemini
        answers 400 "Unknown name additional_properties" for the same key, so the
        shared schema is filtered per provider rather than duplicated.
        """
        if isinstance(schema, dict):
            return {
                k: LLMScreener._gemini_schema(v)
                for k, v in schema.items()
                if k not in ("additionalProperties", "$schema")
            }
        if isinstance(schema, list):
            return [LLMScreener._gemini_schema(v) for v in schema]
        return schema

    def _call_gemini(self, prompt: str, system: str, schema: dict) -> str:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=self._gemini_schema(schema),
                temperature=0.0,
            ),
        )
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            self._record_usage(
                getattr(usage, "prompt_token_count", 0) or 0,
                getattr(usage, "candidates_token_count", 0) or 0,
                getattr(usage, "total_token_count", 0) or 0,
            )
        return resp.text or ""

    def _call_openai(self, prompt: str, system: str, schema: dict) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_result",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        usage = getattr(resp, "usage", None)
        if usage:
            self._record_usage(
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                getattr(usage, "total_tokens", 0) or 0,
            )
        return resp.choices[0].message.content or ""

    def _record_usage(self, inp: int, out: int, total: int) -> None:
        self.last_usage = {"input": inp, "output": out, "total": total or inp + out}
        for key in ("input", "output", "total"):
            self.total_usage[key] += self.last_usage[key]

    def complete_json(self, prompt: str, system: str, schema: dict) -> dict:
        """One structured-JSON call against whichever provider is configured."""
        raw = (
            self._call_gemini(prompt, system, schema)
            if self.provider == "gemini"
            else self._call_openai(prompt, system, schema)
        )
        return _extract_json(raw)

    def analyze(self, post: Post, retries: int = 2) -> LLMAnalysisResult:
        """Classify one post. Raises RuntimeError only after all retries fail."""
        prompt = _build_user_prompt(post)
        last_error: Optional[Exception] = None

        for attempt in range(retries + 1):
            try:
                payload = self.complete_json(prompt, SYSTEM_PROMPT, JSON_SCHEMA)
                result = LLMAnalysisResult.model_validate(payload)
                time.sleep(self.delay)
                return result
            except Exception as exc:
                last_error = exc
                wait = _backoff_for(exc, attempt)
                log.warning(
                    "LLM call failed (%s/%s) for %s, retrying in %ss: %s",
                    attempt + 1, retries + 1, post.url, wait, exc,
                )
                time.sleep(wait)

        raise RuntimeError(f"LLM screening failed for {post.url}: {last_error}")


RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "quota", "rate limit", "too many requests")
OVERLOAD_MARKERS = ("503", "unavailable", "overloaded", "high demand")


def _backoff_for(exc: Exception, attempt: int) -> float:
    """Seconds to wait before the next attempt.

    Rate limits need a wait on the order of the provider's quota window, not the
    1-2-4s that suits a transient 5xx: Gemini's free tier is per-minute, so a
    short backoff just burns the remaining retries instantly. The provider's own
    `retryDelay` hint is used when present.

    A 503 capacity spike also outlasts 1-2-4s - observed killing whole runs - so
    it gets its own middle tier: long enough to ride out a spike, short enough
    that a genuinely dead endpoint still fails the run promptly.
    """
    text = str(exc).lower()
    if any(marker in text for marker in RATE_LIMIT_MARKERS):
        hinted = re.search(r"'?retrydelay'?:\s*'?(\d+)s", text)
        if hinted:
            return min(float(hinted.group(1)) + 2, 120.0)
        return min(30.0 * (attempt + 1), 120.0)
    if any(marker in text for marker in OVERLOAD_MARKERS):
        return min(10.0 * (2 ** attempt), 60.0)
    return float(2 ** attempt)


def _extract_json(raw: str) -> dict:
    """Tolerate fenced or prefixed JSON output."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


# --------------------------------------------------------------------------
# Optional: LLM query expansion
# --------------------------------------------------------------------------

EXPANSION_PROMPT = """You expand a researcher's prompt into search terms for Israeli teen forums (Stips, FXP) and Reddit, for a study on adolescent medical privacy (Type 1 Diabetes and psychiatric care) and the adolescent-autonomy vs parental-involvement tension.

Return strict JSON: {"terms": ["...", "..."]} with 6-12 terms.
Rules: Hebrew terms for the Israeli sites, a few English ones for Reddit. Use the
colloquial phrasing a teenager would actually type, not clinical vocabulary. Each
term is a short phrase (2-5 words). No duplicates, no explanations."""

EXPANSION_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
    "required": ["terms"],
    "additionalProperties": False,
}


def expand_query(prompt: str, provider: Optional[str] = None, limit: int = 12) -> list[str]:
    """Turn a free-text prompt into extra search terms. Returns [] on any failure."""
    if not prompt.strip():
        return []
    try:
        screener = LLMScreener(provider=provider, delay=0.0)
    except Exception as exc:
        log.warning("Query expansion unavailable: %s", exc)
        return []

    # Retries for the same reason analyze() does: a single 503 or rate limit
    # would otherwise silently drop expansion and quietly narrow the whole run.
    terms: list = []
    for attempt in range(3):
        try:
            terms = screener.complete_json(
                prompt, EXPANSION_PROMPT, EXPANSION_SCHEMA).get("terms") or []
            break
        except Exception as exc:
            if attempt == 2:
                log.warning("Query expansion failed: %s", exc)
                return []
            wait = _backoff_for(exc, attempt)
            log.warning("Query expansion retry %s/3 in %ss: %s", attempt + 1, wait, exc)
            time.sleep(wait)

    out, seen = [], set()
    for term in terms:
        term = str(term).strip().strip('"')
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out[:limit]
