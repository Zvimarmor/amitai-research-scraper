"""Article sources (health portals, news, academic) and manual research tags."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from scripts.export_amitai_csv import build_rows
from src.db import Database
from src.fetcher import parse_article
from src.models import Post, RegexScreenResult, Source


# --------------------------------------------------------------------------
# Source routing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.schneider.org.il/?CategoryID=829&ArticleID=2337", Source.SCHNEIDER),
    ("https://www.ynet.co.il/parents/article/skqcamrso", Source.YNET),
    ("https://www.mako.co.il/home-family-kids/healthcare/Article-7cc9001f8e9cc31006.htm", Source.MAKO),
    ("https://in.bgu.ac.il/medicine/Pages/news.aspx", Source.ACADEMIC),
    ("https://www.tau.ac.il/~someone/paper.html", Source.ACADEMIC),
    ("https://stips.co.il/ask/1", Source.STIPS),
])
def test_article_hosts_map_to_their_source(url, expected):
    assert Source.from_url(url) is expected


@pytest.mark.parametrize("url", [
    "https://notynet.co.il/x",                # suffix match must respect the dot
    "https://example.com/?ref=mako.co.il",     # a host named in the query string
    "https://www.mako.co.il.evil.com/x",
])
def test_lookalike_hosts_are_not_article_sources(url):
    assert not Source.from_url(url).is_article


# --------------------------------------------------------------------------
# parse_article
# --------------------------------------------------------------------------

NEWS_HTML = """<html><head>
<meta property="og:title" content="חיי אמא לילד חולה בסוכרת נעורים">
<script type="application/ld+json">{"@type": "BreadcrumbList", "itemListElement": []}</script>
<script type="application/ld+json">{
  "@type": "NewsArticle",
  "headline": "הוא שואל: אמא, עד מתי?",
  "description": "כשהוא לא הסכים לקבל את זריקות האינסולין, עשינו זאת בכוח",
  "datePublished": "2013-02-12T17:41:00+0200",
  "author": [{"@type": "Person", "name": "טל גרוס"}]
}</script></head><body>
<nav><p>תפריט ראשי עם הרבה קישורים שלא קשורים לכתבה בכלל ולא אמורים להיכנס</p></nav>
<article>
  <p>לפני שנה וארבעה חודשים גילינו שליותם שלנו, בן השמונה, יש סוכרת נעורים.</p>
  <p>אני מוצאת את עצמי מגיעה להרבה ימי הולדת כדי לבדוק שהוא לא אוכל יותר מדי ממתקים.</p>
  <p>קצר</p>
</article></body></html>"""


def test_news_article_reads_jsonld_metadata_and_paragraphs():
    post = parse_article(NEWS_HTML, "https://www.mako.co.il/a.htm", "q", Source.MAKO)
    assert post.source is Source.MAKO
    assert post.title == "חיי אמא לילד חולה בסוכרת נעורים"
    assert post.author == "טל גרוס"
    assert post.posted_at == "2013-02-12T17:41:00+0200"
    assert "ימי הולדת" in post.body
    # The standfirst carries the thesis and is kept ahead of the body.
    assert post.body.startswith("כשהוא לא הסכים")
    assert "תפריט ראשי" not in post.body
    assert "קצר" not in post.body.split("\n")


ACADEMIC_HTML = """<html><head>
<meta name="citation_title" content="Autonomy and parental involvement in adolescents with type 1 diabetes">
<meta name="citation_author" content="Cohen, D.">
<meta name="citation_author" content="Levi, R.">
<meta name="citation_publication_date" content="2021/05/03">
<meta name="citation_abstract" content="Adolescents reported conflict with parents over glucose monitoring.">
</head><body><main>
<p>This study examined how adolescents and parents share responsibility for diabetes care.</p>
<p>Parents of younger adolescents reported more checking of glucose logs than parents of older ones.</p>
</main></body></html>"""


def test_academic_page_falls_back_to_citation_meta():
    post = parse_article(ACADEMIC_HTML, "https://x.ac.il/p", "q", Source.ACADEMIC)
    assert post.title.startswith("Autonomy and parental involvement")
    assert post.author == "Cohen, D., Levi, R."
    assert post.posted_at == "2021/05/03"
    assert post.body.startswith("Adolescents reported conflict")
    assert "glucose logs" in post.body


def test_jsonld_graph_and_string_author_are_supported():
    html = """<html><head><script type="application/ld+json">{"@graph": [
      {"@type": "WebPage", "name": "page"},
      {"@type": ["Article"], "headline": "כותרת", "author": "ד\\"ר כהן",
       "datePublished": "2024-01-01"}]}</script></head>
      <body><h1>כותרת מהעמוד</h1><article><p>פסקה ראשונה ארוכה מספיק כדי להיחשב חלק מגוף הכתבה.</p>
      <p>פסקה שנייה ארוכה מספיק כדי להיחשב חלק מגוף הכתבה גם היא.</p></article></body></html>"""
    post = parse_article(html, "https://www.ynet.co.il/a", "q", Source.YNET)
    assert post.author == 'ד"ר כהן'
    assert post.posted_at == "2024-01-01"
    assert post.title == "כותרת מהעמוד"


def test_malformed_jsonld_does_not_break_parsing():
    html = """<html><head><script type="application/ld+json">{not json</script>
      <meta name="author" content="מערכת"><meta property="article:published_time" content="2020-02-02">
      </head><body><h1>כותרת</h1><article><p>פסקה ראשונה ארוכה מספיק כדי להיחשב חלק מגוף הכתבה.</p>
      <p>פסקה שנייה ארוכה מספיק כדי להיחשב חלק מגוף הכתבה גם היא.</p></article></body></html>"""
    post = parse_article(html, "https://www.ynet.co.il/a", "q", Source.YNET)
    assert post.author == "מערכת"
    assert post.posted_at == "2020-02-02"
    assert "פסקה שנייה" in post.body


def test_profile_url_in_article_author_is_not_taken_as_a_name():
    html = """<html><head><meta property="article:author" content="https://facebook.com/someone">
      </head><body><article><p>פסקה ראשונה ארוכה מספיק כדי להיחשב חלק מגוף הכתבה.</p>
      <p>פסקה שנייה ארוכה מספיק כדי להיחשב חלק מגוף הכתבה גם היא.</p></article></body></html>"""
    assert parse_article(html, "https://www.ynet.co.il/a", "q", Source.YNET).author is None


# --------------------------------------------------------------------------
# Storage: migration, metadata, manual tags
# --------------------------------------------------------------------------

@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield Database(Path(tmp) / "t.sqlite3")


def test_existing_database_gains_the_new_columns():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL, source TEXT NOT NULL,
            title TEXT, raw_text TEXT NOT NULL, query TEXT, regex_json TEXT,
            analysis_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO posts (url, content_hash, source, raw_text, created_at, updated_at)"
                     " VALUES ('u', 'h', 'stips', 'text', 'now', 'now')")
        conn.commit()
        conn.close()

        Database(path)
        Database(path)  # second open must not try to add them again

        conn = sqlite3.connect(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        assert {"author", "published_at", "research_category"} <= cols
        assert conn.execute("SELECT raw_text FROM posts").fetchone()[0] == "text"
        conn.close()


def _article(url="https://www.mako.co.il/a.htm", **kw) -> Post:
    defaults = dict(url=url, source=Source.MAKO, title="כותרת",
                    body="גוף הכתבה " * 20, author="טל גרוס", posted_at="2013-02-12")
    return Post(**(defaults | kw))


def test_author_and_date_are_stored_and_survive_a_refetch_without_them(db):
    db.upsert_post(_article())
    db.upsert_post(_article(author=None, posted_at=None))
    row = db.fetch_posts(limit=0)["items"][0]
    assert row["author"] == "טל גרוס"
    assert row["published_at"] == "2013-02-12"


def test_set_category_reports_unknown_urls(db):
    db.upsert_post(_article())
    assert db.set_category("https://www.mako.co.il/a.htm", "parental_distrust")
    assert not db.set_category("https://nowhere.example/x", "parental_distrust")
    assert db.fetch_posts(limit=0)["items"][0]["research_category"] == "parental_distrust"


def test_manual_tag_overrides_rules_and_forces_inclusion_in_export(db):
    # No CGM words, no friction words, regex failed: the rules alone would drop it.
    db.upsert_post(_article(), regex=RegexScreenResult(passed=False))
    assert build_rows(db) == []

    db.set_category("https://www.mako.co.il/a.htm", "autonomy_struggles")
    [row] = build_rows(db)
    assert row["research_category"] == "autonomy_struggles"
    assert row["category_origin"] == "manual"
    assert row["author"] == "טל גרוס"
    assert "manually_tagged" in row["inclusion_reason"]


def test_untagged_rows_keep_rule_based_category(db):
    db.upsert_post(Post(url="https://stips.co.il/ask/2", source=Source.STIPS,
                        title="אין לי כח להחליף חיישן", body="סוכרת נעורים, אני בוכה " * 5))
    [row] = build_rows(db)
    assert row["research_category"] == "t1d_burnout"
    assert row["category_origin"] == "rule"


def test_excluded_tag_drops_a_row_the_rules_would_keep(db):
    # "עוקבים" (followers) is monitoring vocabulary, so the rules let it in.
    url = "https://stips.co.il/ask/3"
    db.upsert_post(Post(url=url, source=Source.STIPS, title="עוקבים באינסטגרם",
                        body="כמה עוקבים יש לכם באינסטגרם? " * 5))
    assert len(build_rows(db)) == 1
    db.set_category(url, "excluded")
    assert build_rows(db) == []
