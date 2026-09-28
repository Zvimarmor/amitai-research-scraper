"""CLI entry point.

    python -m src.main run --prompt "adolescent diabetes privacy" --limit 10
    python -m src.main queries --themes t1d psychiatric
    python -m src.main show --relevant
    python -m src.main stats
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Callable, Optional

from .db import Database
from .fetcher import Fetcher, polite_sleep
from .models import Post, ScreenedPost, Source
from .screener import LLMScreener, expand_query, regex_screen
from .searcher import DEFAULT_SITES, THEMES, Searcher, build_queries

log = logging.getLogger("pipeline")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Third-party HTTP chatter drowns out the pipeline's own log lines.
    for noisy in ("primp", "ddgs", "httpx", "urllib3", "google_genai", "openai"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


def _serialize(item: ScreenedPost) -> dict:
    post = item.post
    return {
        "url": post.url,
        "source": post.source.value,
        "title": post.title,
        "query": post.query,
        "chars": len(post.full_text),
        "comments": len(post.comments),
        "regex_screen": item.regex.model_dump(),
        "analysis": item.analysis.model_dump() if item.analysis else None,
        "error": item.error,
    }


def run_pipeline(
    prompt: str,
    themes: list[str],
    sites: list[str],
    limit: int,
    use_llm: bool = True,
    require_adolescent: bool = False,
    broad: bool = True,
    backend: Optional[str] = None,
    provider: Optional[str] = None,
    db: Optional[Database] = None,
    keywords: Optional[list[str]] = None,
    expand: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> list[ScreenedPost]:
    """Run one collection pass.

    `should_stop` is polled between every search query and every fetch so a
    caller on another thread can halt the run without killing it mid-request.
    `on_event(kind, payload)` reports progress; both are no-ops when omitted.
    """
    db = db or Database()
    stopped = should_stop or (lambda: False)
    emit = on_event or (lambda kind, payload: None)

    extra = [t for t in ([prompt] + list(keywords or [])) if t and t.strip()]
    if expand:
        expanded = expand_query(prompt, provider=provider)
        if expanded:
            log.info("Query expansion added %d terms: %s", len(expanded), ", ".join(expanded))
            emit("expanded", {"terms": expanded})
            extra.extend(expanded)

    queries = build_queries(
        core_themes=themes,
        sites=sites,
        max_results=max(5, limit),
        extra_terms=extra or None,
        include_broad=broad,
    )
    log.info("Generated %d queries across %s", len(queries), ", ".join(sites))

    searcher = Searcher(backend=backend) if backend else Searcher()
    # Over-fetch: many hits will be duplicates or already stored.
    emit("searching", {"queries": len(queries)})
    hits = searcher.search(queries, limit=limit * 3, should_stop=stopped)
    log.info("Search returned %d unique URLs", len(hits))
    emit("search_done", {"urls": len(hits)})

    screener: Optional[LLMScreener] = None
    if use_llm:
        try:
            screener = LLMScreener(provider=provider)
            log.info("LLM screener ready: %s / %s", screener.provider, screener.model)
        except Exception as exc:
            log.error("LLM screener unavailable (%s) - continuing with regex only", exc)

    results: list[ScreenedPost] = []
    with Fetcher() as fetcher:
        for hit in hits:
            if len(results) >= limit or stopped():
                break
            if db.seen_url(hit.url):
                log.debug("Skipping already-stored URL: %s", hit.url)
                continue

            log.info("Fetching [%d/%d] %s", len(results) + 1, limit, hit.url)
            post = fetcher.fetch_post(hit.url, query=hit.query)
            polite_sleep()
            if post is None or len(post.full_text) < 80:
                log.debug("No usable content at %s", hit.url)
                continue
            if db.seen_hash(post.content_hash):
                log.debug("Duplicate content hash, skipping: %s", hit.url)
                continue

            regex = regex_screen(post, require_adolescent=require_adolescent)
            item = ScreenedPost(post=post, regex=regex)

            if regex.passed and screener is not None:
                try:
                    item.analysis = screener.analyze(post)
                except Exception as exc:
                    item.error = str(exc)
                    log.warning("Screening error for %s: %s", hit.url, exc)
            elif not regex.passed:
                log.debug("Regex filter rejected %s (%s)", hit.url, regex.reason)

            db.upsert_post(post, regex=regex, analysis=item.analysis)
            results.append(item)
            emit("post", _serialize(item))

    emit("finished", {"stopped": stopped(), "count": len(results)})
    return results


def cmd_run(args: argparse.Namespace) -> int:
    results = run_pipeline(
        prompt=args.prompt,
        themes=args.themes,
        sites=args.sites,
        limit=args.limit,
        use_llm=not args.no_llm,
        require_adolescent=args.require_adolescent,
        broad=not args.strict_queries,
        backend=args.backend,
        provider=args.provider,
        keywords=args.keywords,
        expand=args.expand,
    )
    payload = [_serialize(r) for r in results]
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    relevant = sum(1 for r in results if r.analysis and r.analysis.is_relevant)
    print(
        f"\n{len(results)} candidates stored | "
        f"{sum(1 for r in results if r.regex.passed)} passed regex | "
        f"{relevant} judged relevant",
        file=sys.stderr,
    )
    return 0


def cmd_rescreen(args: argparse.Namespace) -> int:
    """LLM-screen posts already in the database.

    Lets collection and screening be decoupled: scrape now (slow, network-bound,
    no API key needed), screen later, and re-screen after a prompt change
    without hitting the forums again.
    """
    db = Database()
    screener = LLMScreener(provider=args.provider, delay=args.delay)
    log.info("Rescreening with %s / %s (%.1fs between calls)",
             screener.provider, screener.model, args.delay)

    rows = db.fetch_posts(limit=args.limit, relevant_only=False)["items"]
    targets = [
        r for r in rows
        if (args.all or (r.get("regex_json") or {}).get("passed"))
        and (args.force or not r.get("analysis_json"))
    ]
    log.info("%d of %d stored posts need screening", len(targets), len(rows))

    done = 0
    for row in targets:
        post = Post(
            url=row["url"],
            source=Source(row["source"]),
            title=row["title"] or "",
            body=row["raw_text"] or "",
            query=row["query"] or "",
        )
        try:
            analysis = screener.analyze(post)
        except Exception as exc:
            log.warning("Screening failed for %s: %s", row["url"], exc)
            continue
        db.upsert_post(post, analysis=analysis)
        done += 1
        verdict = "RELEVANT" if analysis.is_relevant else "not relevant"
        log.info("[%d/%d] %s (%.2f) %s", done, len(targets), verdict,
                 analysis.confidence, row["url"])

    print(json.dumps({"screened": done, "candidates": len(targets)}, indent=2))
    return 0


def cmd_queries(args: argparse.Namespace) -> int:
    queries = build_queries(core_themes=args.themes, sites=args.sites)
    for q in queries:
        print(f"[{q.site}] {q.query}")
    print(f"\n{len(queries)} queries", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    rows = Database().fetch_all(limit=args.limit, relevant_only=args.relevant)
    if not args.full:
        for row in rows:
            row.pop("raw_text", None)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    print(json.dumps(Database().stats(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Adolescent medical-privacy research pipeline.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="search -> fetch -> screen -> store")
    run.add_argument("--prompt", default="", help="free-text term added to every site dork")
    run.add_argument("--themes", nargs="+", default=["t1d", "psychiatric"],
                     choices=sorted(THEMES), help="clinical themes to search")
    run.add_argument("--sites", nargs="+", default=list(DEFAULT_SITES))
    run.add_argument("--limit", type=int, default=10, help="candidate posts to process")
    run.add_argument("--no-llm", action="store_true", help="regex pass only (no API key needed)")
    run.add_argument("--require-adolescent", action="store_true",
                     help="regex pass also requires an age/adolescent marker")
    run.add_argument("--strict-queries", action="store_true",
                     help="only high-precision dorks (both terms quoted); lower recall")
    run.add_argument("--keywords", nargs="*", default=None,
                     help="extra literal search terms, added as their own dorks")
    run.add_argument("--expand", action="store_true",
                     help="use the LLM to expand --prompt into extra search terms")
    run.add_argument("--backend", default=None,
                     help="comma-delimited ddgs engines, e.g. 'brave, google' (default: auto)")
    run.add_argument("--provider", choices=["gemini", "openai"], default=None)
    run.set_defaults(func=cmd_run)

    q = sub.add_parser("queries", help="print generated dorks without running them")
    q.add_argument("--themes", nargs="+", default=["t1d", "psychiatric"], choices=sorted(THEMES))
    q.add_argument("--sites", nargs="+", default=list(DEFAULT_SITES))
    q.set_defaults(func=cmd_queries)

    rescreen = sub.add_parser(
        "rescreen", help="run the LLM pass over stored posts that passed the regex filter")
    rescreen.add_argument("--limit", type=int, default=100)
    rescreen.add_argument("--delay", type=float, default=4.0,
                          help="seconds between LLM calls; the default paces to ~15 "
                               "requests/min to stay under free-tier quotas")
    rescreen.add_argument("--all", action="store_true",
                          help="screen every stored post, not just regex survivors "
                               "(use to measure what the regex filter discards)")
    rescreen.add_argument("--force", action="store_true",
                          help="re-screen posts that already have a verdict")
    rescreen.add_argument("--provider", choices=["gemini", "openai"], default=None)
    rescreen.set_defaults(func=cmd_rescreen)

    show = sub.add_parser("show", help="dump stored rows as JSON")
    show.add_argument("--limit", type=int, default=25)
    show.add_argument("--relevant", action="store_true")
    show.add_argument("--full", action="store_true", help="include raw_text")
    show.set_defaults(func=cmd_show)

    stats = sub.add_parser("stats", help="row counts")
    stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        log.error("Fatal: %s", exc, exc_info=args.verbose)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
