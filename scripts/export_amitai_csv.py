"""Build Amitai's CGM-monitoring CSV from whatever is currently in the database.

Separate from the generic /api/posts/export because this set is defined by the
research question rather than by a verdict: a post belongs here if it survived
the co-occurrence gate, names CGM hardware, or uses remote-monitoring language.
A manually tagged row (see `src.main ingest --category` / `tag`) is always
included and keeps its tag, except the tag "excluded", which drops it. Rows carry why they were included and whether an
LLM verdict exists yet, so a corpus that is only partly screened is still
readable rather than looking as though everything was judged irrelevant.

    .venv/bin/python -m scripts.export_amitai_csv [--out PATH] [--relevant-only]
"""
from __future__ import annotations

import argparse
import csv
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.db import Database
from src.server import CSV_COLUMNS, _shape

CGM_TERMS = [
    "דקסקום", "חיישן ליברה", "ליברה 2", "ליברה 3", "סנסור", "חיישן",
    "מד סוכר", "מד סוכר רציף", "משאבת סוכר", "משאבת אינסולין",
    "אפליקציית סוכר", "התראות סוכר", "ניטור רציף", "בלוטוס",
    "dexcom", "libre", "cgm", "glucose monitor", "insulin pump",
]

FRICTION_TERMS = [
    "מעקב", "ניטור", "עוקבים", "עוקבים אחרי", "בודקים לי", "רואים לי",
    "התראות להורים", "מפקחים", "חופרים", "מציקים", "צפצוף", "צפצופים",
    "מצפצף", "התראה", "התראות", "התראות בלילה", "לכבות", "לנתק",
    "כיביתי", "ניתקתי", "איך להסתיר", "הסתרת סוכר", "שיתוף נתונים",
    "קוד לאפליקציה",
    "monitoring", "tracking", "watching", "alerts", "alarms", "beeping",
]

T1D_TERMS = ["סוכרת", "סכרת", "אינסולין", "diabetes", "insulin"]

BURNOUT_TERMS = [
    "אין לי כח", "אין לי כוח", "בוכה", "נמאס לי", "דחיתי", "סקיירד",
    "מפחדת", "מפחד", "ההתמודדות שלי", "לא מאוזנת", "לא מאוזן",
]

RECORD_PRIVACY_TERMS = [
    "תיק רפואי", "תיק הרפואי", "ויתור סודיות", "סודיות רפואית", "חיסיון",
    "רשומות בתיק", "רשום בתיק", "נרשם בתיק",
]

# Manual tag that removes a row from the export (noise the keyword rules let in,
# e.g. Instagram "followers" matching the monitoring vocabulary).
EXCLUDED = "excluded"

EXTRA_COLUMNS = ["research_category", "category_origin", "author", "published_at",
                 "cgm_terms", "friction_terms", "inclusion_reason", "screening_status"]


def _category(title: str, text: str, cgm: list[str]) -> str:
    """Rule-based bucket so the null finding reads cleanly, screened or not.

    Distress wins over hardware: a teen crying about changing a sensor is a
    burnout quote, not a device question. Burnout needs diabetes in the title
    so a stressed first-aid student who mentions diabetes in passing stays out.
    """
    t1d = bool(cgm) or bool(_found(text, T1D_TERMS))
    t1d_asker = bool(_found(title, CGM_TERMS + T1D_TERMS))
    if t1d_asker and _found(text, BURNOUT_TERMS):
        return "t1d_burnout"
    if cgm:
        return "cgm_hardware"
    if _found(text, RECORD_PRIVACY_TERMS):
        return "digital_record_privacy"
    if t1d:
        return "t1d_general"
    return "other"


def _found(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t.lower() in low]


def build_rows(db: Database, relevant_only: bool = False) -> list[dict]:
    rows = db.fetch_posts(limit=0, relevant_only=False, analyzed_only=False)["items"]
    out = []
    for row in rows:
        text = f"{row.get('title') or ''} {row.get('raw_text') or ''}"
        cgm = _found(text, CGM_TERMS)
        friction = _found(text, FRICTION_TERMS)
        passed = bool((row.get("regex_json") or {}).get("passed"))
        analysis = row.get("analysis_json") or {}
        manual = row.get("research_category")

        if manual == EXCLUDED:
            continue
        if not (passed or cgm or friction or manual):
            continue
        if relevant_only and not analysis.get("is_relevant"):
            continue

        reasons = []
        if manual:
            reasons.append("manually_tagged")
        if cgm:
            reasons.append("cgm_device_mentioned")
        if friction:
            reasons.append("monitoring_friction_language")
        if passed:
            reasons.append("passed_regex_cooccurrence")
        if analysis.get("is_relevant"):
            reasons.append("llm_judged_relevant")

        item = _shape(row)
        # A researcher's tag beats the keyword rules, which only see wording.
        item["research_category"] = manual or _category(row.get("title") or "", text, cgm)
        item["category_origin"] = "manual" if manual else "rule"
        item["author"] = row.get("author") or ""
        item["published_at"] = row.get("published_at") or ""
        item["cgm_terms"] = " | ".join(cgm)
        item["friction_terms"] = " | ".join(friction)
        item["inclusion_reason"] = " | ".join(reasons)
        item["screening_status"] = "llm_screened" if analysis else "awaiting_llm_screening"
        for key in ("key_quotes", "medical_terms", "privacy_terms"):
            item[key] = " | ".join(item.get(key) or [])
        out.append(item)

    # Unscreened first, then relevant, then by confidence: the rows needing a
    # human or a verdict sit at the top rather than buried under settled ones.
    out.sort(key=lambda d: (d["screening_status"] == "llm_screened",
                            not d.get("is_relevant"),
                            -(d.get("confidence") or 0)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/amitai_dexcom_monitoring.csv")
    ap.add_argument("--relevant-only", action="store_true",
                    help="keep only rows the LLM judged relevant")
    args = ap.parse_args()

    rows = build_rows(Database(), relevant_only=args.relevant_only)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS + EXTRA_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    dest = pathlib.Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # BOM so Excel on Windows reads the Hebrew columns as UTF-8.
    dest.write_text("﻿" + buf.getvalue(), encoding="utf-8")

    screened = sum(1 for r in rows if r["screening_status"] == "llm_screened")
    print(f"{dest}: {len(rows)} rows")
    print(f"  llm_screened          : {screened}  (relevant: "
          f"{sum(1 for r in rows if r.get('is_relevant'))})")
    print(f"  awaiting_llm_screening: {len(rows) - screened}")
    print(f"  mentioning CGM kit    : {sum(1 for r in rows if r['cgm_terms'])}")
    print(f"  friction language     : {sum(1 for r in rows if r['friction_terms'])}")
    cats: dict[str, int] = {}
    for r in rows:
        cats[r["research_category"]] = cats.get(r["research_category"], 0) + 1
    for cat, n in sorted(cats.items(), key=lambda kv: -kv[1]):
        print(f"  category {cat:<22}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
