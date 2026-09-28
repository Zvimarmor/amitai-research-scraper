"""Search orchestration: turn research themes into site-scoped dorks and run them.

Uses DuckDuckGo (via the `ddgs` package, formerly `duckduckgo_search`). The
generator is deterministic so a run can be reproduced from the query list alone.
"""
from __future__ import annotations

import concurrent.futures
import itertools
import logging
import random
import time
from typing import Callable, Iterable, Optional

from .config import SEARCH_BACKEND, SEARCH_DELAY, SEARCH_REGION, SEARCH_TIMEOUT
from .models import ScrapingQuery, SearchHit, Source

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Theme vocabulary. Hebrew first (FXP / Stips), English for reddit.
# --------------------------------------------------------------------------

THEMES: dict[str, dict[str, list[str]]] = {
    "t1d": {
        "he": [
            "סוכרת נעורים",
            "סוכרת סוג 1",
            "משאבת אינסולין",
            "חיישן סוכר",
            "אינסולין בבית ספר",
            # CGM hardware and the companion apps, which are where remote
            # parental monitoring actually happens.
            "דקסקום",
            # Never bare "ליברה": it returns YSL perfume threads, which cost a
            # fetch each before the regex gate discards them.
            "חיישן ליברה",
            "ליברה סוכרת",
            "סנסור סוכר",
            "מד סוכר רציף",
            "אפליקציית סוכרת",
            "התראות סוכר",
            "מד סוכר",
            # How teenagers actually phrase the friction.
            "חיישן מצפצף",
            "צפצופים בלילה סוכר",
            "לכבות התראות סוכר",
        ],
        "en": [
            "type 1 diabetes teen",
            "T1D teenager parents",
            "insulin pump teen privacy",
            "dexcom teen parents",
            "freestyle libre teen",
            "dexcom follow parents",
        ],
    },
    "psychiatric": {
        "he": [
            "טיפול פסיכולוגי לנוער",
            "פסיכיאטר לנוער",
            "טיפול נפשי בלי ההורים",
            "ריטלין",
            "אשפוז פסיכיאטרי נוער",
        ],
        "en": [
            "teen therapy without parents knowing",
            "adolescent psychiatric confidentiality",
            "teen antidepressants parents",
        ],
    },
    "privacy": {
        "he": [
            "סודיות רפואית קטין",
            "בלי ידיעת ההורים",
            "ההורים לא יודעים",
            "הרופא סיפר להורים",
            "פרטיות רפואית נוער",
            "זכות קטין לסודיות",
            # Remote CGM monitoring: the parent watches continuously rather than
            # being told after the fact, so the phrasing differs from disclosure.
            "ההורים עוקבים אחרי",
            "מעקב הורים סוכר",
            "ניטור מרחוק סוכרת",
            "התראות להורים",
            "רואים לי בטלפון",
            "ההורים חופרים סוכר",
            "שיתוף נתונים הורים",
            "איך להסתיר סוכר",
        ],
        "en": [
            "doctor told my parents",
            "minor medical confidentiality",
            "without my parents knowing",
            "parents watching my dexcom",
            "share glucose data parents",
        ],
    },
    "autonomy": {
        "he": [
            "הסכמה מדעת קטין",
            "החלטות רפואיות בגיל 16",
            "לנהל את הטיפול לבד",
        ],
        "en": [
            "medical autonomy minor",
            "manage my own treatment teen",
        ],
    },
}

SITE_LANGUAGE = {
    "fxp.co.il": "he",
    "stips.co.il": "he",
    "reddit.com": "en",
}

DEFAULT_SITES = ("fxp.co.il", "stips.co.il", "reddit.com")


def _terms(theme: str, language: str) -> list[str]:
    return THEMES.get(theme, {}).get(language, [])


def _interleave(*groups: list[ScrapingQuery]) -> list[ScrapingQuery]:
    """Round-robin the tiers together, preserving order within each tier."""
    out: list[ScrapingQuery] = []
    for row in itertools.zip_longest(*groups):
        out.extend(q for q in row if q is not None)
    return out


def build_queries(
    core_themes: Iterable[str],
    modifier_themes: Iterable[str] = ("privacy", "autonomy"),
    sites: Iterable[str] = DEFAULT_SITES,
    max_results: int = 10,
    extra_terms: Optional[Iterable[str]] = None,
    include_broad: bool = True,
) -> list[ScrapingQuery]:
    """Cross core clinical themes with privacy/autonomy modifiers, per site.

    Three tiers are generated:

    1. `site:<domain> "<clinical term>" "<privacy term>"` - high precision, but
       two quoted Hebrew phrases co-occurring is rare, so most of these return
       nothing.
    2. `site:<domain> "<clinical term>" <privacy term>` - modifier unquoted.
    3. `site:<domain> "<clinical term>"` - clinical term alone.

    The tiers are interleaved rather than concatenated. Run sequentially, a few
    hundred empty tier-1 dorks would burn the whole run before reaching one that
    yields, so round-robin keeps precise queries first *and* reaches productive
    ones within the first few requests. Tiers 2-3 are dropped if
    `include_broad` is False, which trades yield for precision.
    """
    sites = list(sites)
    core_themes = list(core_themes)
    modifier_themes = list(modifier_themes)
    extra = list(extra_terms or [])
    theme_label = "+".join(core_themes)

    all_queries: list[ScrapingQuery] = []
    seen: set[str] = set()

    def add(bucket: list[ScrapingQuery], query: str, site: str, lang: str,
            theme: str = "") -> None:
        if query in seen:
            return
        seen.add(query)
        bucket.append(ScrapingQuery(
            query=query, site=site, theme=theme or theme_label,
            language=lang, max_results=max_results,
        ))

    for site in sites:
        lang = SITE_LANGUAGE.get(site, "he")
        core_terms = list(itertools.chain.from_iterable(_terms(t, lang) for t in core_themes))
        mod_terms = list(itertools.chain.from_iterable(_terms(t, lang) for t in modifier_themes))
        if not core_terms:
            log.warning("No terms for themes %s in language %s", core_themes, lang)
            continue

        precise: list[ScrapingQuery] = []
        loose: list[ScrapingQuery] = []
        bare: list[ScrapingQuery] = []

        for core, mod in itertools.product(core_terms, mod_terms):
            add(precise, f'site:{site} "{core}" "{mod}"', site, lang)
        if not mod_terms:
            for core in core_terms:
                add(precise, f'site:{site} "{core}"', site, lang)

        if include_broad:
            for core, mod in itertools.product(core_terms, mod_terms):
                add(loose, f'site:{site} "{core}" {mod}', site, lang)
            for core in core_terms:
                add(bare, f'site:{site} "{core}"', site, lang)

        for term in extra:
            add(bare, f'site:{site} "{term}"', site, lang, theme="custom")

        all_queries.extend(_interleave(bare, precise, loose))

    return all_queries


def _load_backend():
    """Import whichever DuckDuckGo client is installed."""
    try:
        from ddgs import DDGS  # type: ignore

        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS  # type: ignore

        return DDGS
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Neither `ddgs` nor `duckduckgo_search` is installed. "
            "Run: pip install ddgs"
        ) from exc


class Searcher:
    """Runs generated dorks against DuckDuckGo with polite pacing."""

    def __init__(
        self,
        delay: float = SEARCH_DELAY,
        region: str = SEARCH_REGION,
        backend: str = SEARCH_BACKEND,
        query_timeout: float = SEARCH_TIMEOUT,
    ):
        self.delay = delay
        self.region = region
        self.backend = backend
        self.query_timeout = query_timeout
        self._ddgs_cls = _load_backend()

    def run_query(self, query: ScrapingQuery) -> list[SearchHit]:
        """Run one dork, bounded by `query_timeout`.

        ddgs queries several engines per call and a throttled one can hang far
        longer than its own timeout, so the call is run on a worker thread with
        a hard deadline rather than trusted to return.
        """
        # Deliberately not a `with` block: ThreadPoolExecutor.__exit__ calls
        # shutdown(wait=True), which blocks until the hung worker returns and so
        # defeats the very timeout this method exists to enforce. The executor is
        # shut down without waiting and the daemon thread is abandoned; it dies
        # with the process.
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="search"
        )
        future = pool.submit(self._run_query_blocking, query)
        try:
            return future.result(timeout=self.query_timeout)
        except concurrent.futures.TimeoutError:
            log.warning("Search timed out after %.0fs: %s", self.query_timeout, query.query)
            return []
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Connection-level failures are transient under load: the metasearch
    # backends refuse connections intermittently, and a second attempt a few
    # seconds later usually succeeds. "No results" is not retried - it is an
    # answer, not a failure.
    TRANSIENT = ("connecterror", "connection", "timeout", "timed out",
                 "temporarily", "reset by peer")

    def _run_query_blocking(self, query: ScrapingQuery, attempts: int = 2) -> list[SearchHit]:
        for attempt in range(attempts):
            hits, error = self._attempt_query(query)
            if hits or error is None:
                return hits
            if attempt + 1 >= attempts or not any(
                marker in error.lower() for marker in self.TRANSIENT
            ):
                return hits
            log.debug("Retrying after transient search error: %s", error)
            time.sleep(2.0 + random.uniform(0, 2.0))
        return []

    def _attempt_query(self, query: ScrapingQuery) -> tuple[list[SearchHit], Optional[str]]:
        """Returns (hits, error-text-or-None). Never raises."""
        hits: list[SearchHit] = []
        try:
            with self._ddgs_cls() as ddgs:
                try:
                    results = ddgs.text(
                        query.query,
                        region=self.region,
                        backend=self.backend,
                        max_results=query.max_results,
                    )
                except Exception as exc:
                    if self.backend == "auto" or "backends do not exist" not in str(exc):
                        raise
                    # ddgs disables engines on the fly; drop back to whatever is up.
                    log.warning("Backend %r unavailable, falling back to auto", self.backend)
                    self.backend = "auto"
                    results = ddgs.text(
                        query.query,
                        region=self.region,
                        backend="auto",
                        max_results=query.max_results,
                    )
                for r in results or []:
                    url = r.get("href") or r.get("url") or ""
                    if not url:
                        continue
                    hits.append(
                        SearchHit(
                            url=url,
                            title=r.get("title", "") or "",
                            snippet=r.get("body", "") or r.get("snippet", "") or "",
                            source=Source.from_url(url),
                            query=query.query,
                        )
                    )
        except Exception as exc:
            # ddgs raises instead of returning [] when nothing matches. A dork
            # with no hits is the normal case here, not an error worth warning
            # about; rate limits and transient backend errors are.
            if "no results" in str(exc).lower():
                log.debug("No results: %s", query.query)
                return hits, None
            log.warning("Search failed for %r: %s", query.query, exc)
            return hits, str(exc)
        return hits, None

    def search(
        self,
        queries: list[ScrapingQuery],
        limit: Optional[int] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_hit: Optional[Callable[[int, int], None]] = None,
    ) -> list[SearchHit]:
        """Run queries round-robin-ish, de-duplicating URLs, until `limit` hits.

        `should_stop` is polled between queries so a long search can be halted
        from another thread without waiting for the whole query list.
        """
        collected: list[SearchHit] = []
        seen: set[str] = set()

        for i, query in enumerate(queries):
            if limit is not None and len(collected) >= limit:
                break
            if should_stop and should_stop():
                log.info("Search halted by caller after %d queries", i)
                break
            if i:
                time.sleep(self.delay + random.uniform(0, 1.5))
            log.info("Searching: %s", query.query)
            for hit in self.run_query(query):
                key = hit.url.split("#")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                collected.append(hit)
                if on_hit:
                    on_hit(i + 1, len(collected))
                if limit is not None and len(collected) >= limit:
                    break
        return collected
