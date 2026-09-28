"""Build Amitai's CGM-monitoring CSV from whatever is currently in the database.

Separate from the generic /api/posts/export because this set is defined by the
research question rather than by a verdict: a post belongs here if it survived
the co-occurrence gate, names CGM hardware, or uses remote-monitoring language.
Rows carry why they were included and whether an LLM verdict exists yet, so a
corpus that is only partly screened is still readable rather than looking as
though everything was judged irrelevant.

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

EXTRA_COLUMNS = ["cgm_terms", "friction_terms", "inclusion_reason", "screening_status"]


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

        if not (passed or cgm or friction):
            continue
        if relevant_only and not analysis.get("is_relevant"):
            continue

        reasons = []
        if cgm:
            reasons.append("cgm_device_mentioned")
        if friction:
            reasons.append("monitoring_friction_language")
        if passed:
            reasons.append("passed_regex_cooccurrence")
        if analysis.get("is_relevant"):
            reasons.append("llm_judged_relevant")

        item = _shape(row)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
