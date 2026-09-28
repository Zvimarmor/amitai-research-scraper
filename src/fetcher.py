"""HTTP fetching (curl_cffi with Chrome impersonation) and per-site parsers.

Site markup changes often, so every parser tries a list of candidate CSS
selectors and falls back to a generic text extraction rather than raising.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

from .config import FETCH_MAX_DELAY, FETCH_MIN_DELAY, IMPERSONATE, REQUEST_TIMEOUT
from .models import Post, Source

log = logging.getLogger(__name__)

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None


HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}

_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    text = _WS.sub(" ", text or "")
    text = _NL.sub("\n\n", text)
    return text.strip()


def polite_sleep(min_delay: float = FETCH_MIN_DELAY, max_delay: float = FETCH_MAX_DELAY) -> None:
    time.sleep(random.uniform(min_delay, max(min_delay, max_delay)))


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _select_texts(
    soup: BeautifulSoup, selectors: list[str], limit: int = 60, min_len: int = 15
) -> list[str]:
    """Return cleaned texts for the first selector that matches anything."""
    for sel in selectors:
        try:
            nodes = soup.select(sel)
        except Exception:
            continue
        if not nodes:
            continue
        texts = [clean_text(n.get_text("\n", strip=True)) for n in nodes[:limit]]
        texts = [t for t in texts if len(t) >= min_len]
        if texts:
            return texts
    return []


class Fetcher:
    """Session-backed fetcher with browser TLS/JA3 impersonation."""

    def __init__(self, impersonate: str = IMPERSONATE, timeout: float = REQUEST_TIMEOUT):
        if curl_requests is None:
            raise RuntimeError("curl_cffi is not installed. Run: pip install curl_cffi")
        self.impersonate = impersonate
        self.timeout = timeout
        self.session = curl_requests.Session(impersonate=impersonate, headers=HEADERS)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, url: str, retries: int = 2) -> Optional[str]:
        """GET a URL, returning body text or None. Never raises on network error."""
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except Exception as exc:
                log.warning("Request error (%s/%s) for %s: %s", attempt + 1, retries + 1, url, exc)
                polite_sleep()
                continue

            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 403, 503):
                wait = 5 * (attempt + 1)
                log.warning("HTTP %s for %s - backing off %ss", resp.status_code, url, wait)
                time.sleep(wait)
                continue
            log.warning("HTTP %s for %s", resp.status_code, url)
            return None
        return None

    # -- site dispatch --------------------------------------------------

    def fetch_post(self, url: str, query: str = "") -> Optional[Post]:
        source = Source.from_url(url)
        try:
            if source is Source.REDDIT:
                return self._fetch_reddit(url, query)
            html = self.get(url)
            if not html:
                return None
            if source is Source.FXP:
                return parse_fxp(html, url, query)
            if source is Source.STIPS:
                return parse_stips(html, url, query)
            return parse_generic(html, url, query, source)
        except Exception as exc:
            log.warning("Parse failed for %s: %s", url, exc)
            return None

    def _fetch_reddit(self, url: str, query: str) -> Optional[Post]:
        """Prefer Reddit's JSON view; fall back to server-rendered old.reddit HTML.

        `www.reddit.com/*.json` answers 403 to browser-like clients and
        `old.reddit.com` may silently serve HTML instead of JSON, so the response
        is sniffed before being parsed. The modern UI renders client-side and is
        useless to us, hence old.reddit for the HTML path.
        """
        path = urlparse(url).path.rstrip("/")
        for host in ("old.reddit.com", "www.reddit.com"):
            json_url = urlunparse(("https", host, path + "/.json", "", "limit=100", ""))
            body = self.get(json_url, retries=0)
            if not body or body.lstrip()[:1] not in ("{", "["):
                continue
            try:
                return parse_reddit_json(json.loads(body), url, query)
            except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
                log.warning("Reddit JSON parse failed for %s: %s", json_url, exc)

        html = self.get(urlunparse(("https", "old.reddit.com", path + "/", "", "", "")))
        if not html:
            return None
        post = parse_reddit_html(html, url, query)
        if _is_reddit_interstitial(post):
            log.warning(
                "Reddit served its anti-bot interstitial for %s - this IP needs a "
                "different network or Reddit OAuth credentials.", url
            )
            return None
        return post


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

FXP_TITLE_SELECTORS = ["h1.p-title-value", "h1.title", "span.threadtitle", "h1", "title"]
FXP_POST_SELECTORS = [
    "div[id^=post_message_]",        # vBulletin 4 (what fxp.co.il runs today)
    "blockquote.postcontent",
    "div.postcontent",
    "div.bbWrapper",                 # XenForo 2, in case of a platform migration
    "article.message-body",
    "div.post_message",
]

# Quoted-reply blocks duplicate earlier posts; drop them before extracting.
FXP_QUOTE_SELECTORS = ["div.bbcode_container", "div.bbcode_quote"]

STIPS_TITLE_SELECTORS = ["h1", "div.question-title", "meta[property='og:title']", "title"]
# Stips renders the question in the page header and each answer as
# div.article div.textWrapper > div.text. Answer selectors are scoped to
# div.article so the "related questions" sidebar (div.item) is not picked up.
STIPS_QUESTION_SELECTORS = [
    "div.question-text", "div.q-text", "div.question__body", "div.article div.question-body",
]
STIPS_ANSWER_SELECTORS = [
    "div.article div.textWrapper div.text",
    "div.article div.text",
    "div.answer-text",
    "div.answer__body",
]


def _meta_or_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for sel in selectors:
        node = soup.select_one(sel)
        if not node:
            continue
        if node.name == "meta":
            content = node.get("content", "")
            if content:
                return clean_text(content)
            continue
        text = clean_text(node.get_text(" ", strip=True))
        if text:
            return text
    return ""


def parse_fxp(html: str, url: str, query: str = "") -> Post:
    """FXP threads: first post is the body, later posts are the discussion."""
    soup = _soup(html)
    title = _meta_or_text(soup, FXP_TITLE_SELECTORS)
    for sel in FXP_QUOTE_SELECTORS:
        for node in soup.select(sel):
            node.decompose()
    messages = _select_texts(soup, FXP_POST_SELECTORS)
    body = messages[0] if messages else ""
    comments = messages[1:] if len(messages) > 1 else []
    if not body:
        body = _generic_body(soup)
    return Post(
        url=url, source=Source.FXP, title=title, body=body, comments=comments, query=query
    )


def parse_stips(html: str, url: str, query: str = "") -> Post:
    """Stips pages: one question plus N answers."""
    soup = _soup(html)
    title = _meta_or_text(soup, STIPS_TITLE_SELECTORS)
    question = _select_texts(soup, STIPS_QUESTION_SELECTORS)
    # Stips answers are often one or two words ("כן"), so the floor is low here.
    answers = _select_texts(soup, STIPS_ANSWER_SELECTORS, min_len=2)
    body = question[0] if question else ""
    if not body and not answers:
        body = _generic_body(soup)
    return Post(
        url=url, source=Source.STIPS, title=title, body=body, comments=answers, query=query
    )


def parse_reddit_json(payload, url: str, query: str = "") -> Post:
    """Parse the `<permalink>.json` listing pair (post listing, comment listing)."""
    post_listing = payload[0] if isinstance(payload, list) else payload
    children = post_listing.get("data", {}).get("children", [])
    data = children[0].get("data", {}) if children else {}

    title = data.get("title", "") or ""
    body = data.get("selftext", "") or ""
    author = data.get("author")

    comments: list[str] = []
    if isinstance(payload, list) and len(payload) > 1:
        for child in payload[1].get("data", {}).get("children", []):
            cdata = child.get("data", {})
            text = (cdata.get("body") or "").strip()
            if text and text not in ("[deleted]", "[removed]"):
                comments.append(clean_text(text))

    return Post(
        url=url,
        source=Source.REDDIT,
        title=clean_text(title),
        body=clean_text(body),
        comments=comments,
        author=author,
        query=query,
    )


REDDIT_HTML_TITLE_SELECTORS = ["div.top-matter a.title", "a.title", "h1", "title"]

# Reddit answers blocked clients with a generic landing page instead of a 4xx.
REDDIT_BLOCK_MARKERS = ("welcome to reddit", "ברוכים הבאים")


def _is_reddit_interstitial(post: Post) -> bool:
    title = post.title.strip().lower()
    return (
        not post.comments
        and len(post.body) < 400
        and any(marker in title for marker in REDDIT_BLOCK_MARKERS)
    )


def parse_reddit_html(html: str, url: str, query: str = "") -> Post:
    """Parse an old.reddit thread page (server-rendered, unlike the modern UI)."""
    soup = _soup(html)
    title = _meta_or_text(soup, REDDIT_HTML_TITLE_SELECTORS)

    body = ""
    expando = soup.select_one("div.expando div.usertext-body div.md")
    if expando:
        body = clean_text(expando.get_text("\n", strip=True))

    comments: list[str] = []
    for node in soup.select("div.commentarea div.entry div.usertext-body div.md")[:100]:
        text = clean_text(node.get_text("\n", strip=True))
        if text and text not in ("[deleted]", "[removed]"):
            comments.append(text)

    if not body and not comments:
        body = _generic_body(soup)

    return Post(
        url=url, source=Source.REDDIT, title=title, body=body,
        comments=comments, query=query,
    )


def _generic_body(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    main = soup.select_one("main") or soup.select_one("article") or soup.body or soup
    return clean_text(main.get_text("\n", strip=True))


def parse_generic(html: str, url: str, query: str = "", source: Source = Source.OTHER) -> Post:
    soup = _soup(html)
    title = _meta_or_text(soup, ["h1", "meta[property='og:title']", "title"])
    return Post(url=url, source=source, title=title, body=_generic_body(soup), query=query)
