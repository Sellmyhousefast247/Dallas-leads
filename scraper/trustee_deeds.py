#!/usr/bin/env python3
"""Dallas County recorded SUBSTITUTE TRUSTEE'S DEEDS → data/trustee_deeds.csv

Post-sale results feed for the surplus-funds (overage) system: on a substitute
trustee's deed the grantee is the winning bidder and the consideration is the
winning bid. This runs AFTER scraper/fetch.py in the daily workflow and never
touches the main pipeline's files.

Design notes
------------
* Reuses fetch.py's proven GovOS plumbing: fresh browser context per search,
  homepage warmup, quickSearch + _docTypes=<CODE>, row-stability ready check.
* The trustee-deed doc-type CODE is NOT hardcoded. Each run self-verifies it:
  candidate codes (env TRUSTEE_DEED_CODES or the built-in list) are probed and
  a code is accepted only when the rows it returns actually say
  "...TRUSTEE...DEED" (and not "APPOINTMENT") in the DocType column. Confirmed
  codes are cached in data/doc_code_cache.json. If nothing confirms, the run
  writes an empty CSV plus a loud warning in data/trustee_deeds_summary.json —
  it never guesses and never breaks the daily workflow.
* Consideration (sale price) is not in the results grid; it is read from each
  deed's detail page when the page states it. Absent amounts stay blank —
  the downstream underwriter treats blank as unknown, never as $0.
"""
from __future__ import annotations

import csv
import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch  # noqa: E402  (main-guarded; safe to import)
from fetch import (  # noqa: E402
    PAGE_SIZE, log, normalize_date,
)

# GovOS counties share this scraper; each is probed for its own doc codes.
GOVOS_COUNTIES = {
    "dallas": "https://dallas.tx.publicsearch.us",
    "bexar": "https://bexar.tx.publicsearch.us",
}
CLERK_BASE_URL = GOVOS_COUNTIES["dallas"]   # per-run override in main()
CLERK_RESULTS = f"{CLERK_BASE_URL}/results"
from playwright.sync_api import sync_playwright  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT_CSV = DATA / "trustee_deeds.csv"
SUMMARY = DATA / "trustee_deeds_summary.json"
CODE_CACHE = DATA / "doc_code_cache.json"

LOOKBACK_DAYS = int(os.environ.get("DEED_LOOKBACK_DAYS", "35"))
CERT_LAG_DAYS = 3
MAX_PAGES = 10                      # 500 rows/code is plenty for a month of deeds
MAX_DETAIL_VISITS = int(os.environ.get("DEED_DETAIL_CAP", "80"))
CANDIDATE_CODES = [c.strip() for c in os.environ.get(
    "TRUSTEE_DEED_CODES",
    # TD confirmed from the portal's own doc-type table (2026-10-18):
    #   TD = "TRUSTEE'S/SUBSTITUTE TRUSTEE'S DEED", TR DEED = "TRUSTEE DEED"
    "TD,TR DEED"
).split(",") if c.strip()]

DEED_RE = re.compile(r"TRUSTEE.{0,12}DEED|DEED.{0,12}TRUSTEE", re.IGNORECASE)
APPT_RE = re.compile(r"APPOINT", re.IGNORECASE)
CONSIDERATION_RE = re.compile(
    r"(?:CONSIDERATION|SALES?\s+PRICE|AMOUNT)\s*[:\-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE)

FIELDS = ["instrument_number", "recording_date", "sale_date", "grantor", "grantee",
          "beneficiary", "consideration", "property_address", "parcel_id",
          "doc_type", "legal", "clerk_url"]

# Reuse the exact grid column map + ready JS from the proven scraper
_SC = fetch.ClerkScraper if hasattr(fetch, "ClerkScraper") else None
if _SC is None:  # fall back to whatever the class is named
    _SC = next(getattr(fetch, n) for n in dir(fetch)
               if n.endswith("Scraper") and hasattr(getattr(fetch, n), "COL_DOCTYPE"))
COL = _SC
READY_JS = _SC._READY_JS
UA = _SC._UA


def build_url(doc_code: str, start: datetime, end: datetime, offset: int = 0) -> str:
    params = {
        "department": "RP", "searchType": "quickSearch",
        "keywordSearch": "false", "searchOcrText": "false",
        "_docTypes": doc_code,
        "recordedDateRange": f"{start.strftime('%Y%m%d')},{end.strftime('%Y%m%d')}",
        "limit": PAGE_SIZE, "offset": offset,
    }
    return f"{CLERK_RESULTS}?{urlencode(params)}"


def parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []
    out = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= COL.COL_DOCNUM:
            continue
        def cell(i):
            return cells[i].get_text(strip=True) if i < len(cells) else ""
        doc_num = cell(COL.COL_DOCNUM)
        if not doc_num:
            continue
        url = ""
        cb = tr.find("input", id=re.compile(r"table-checkbox-(\d+)"))
        if cb and cb.get("id"):
            m = re.search(r"table-checkbox-(\d+)", cb["id"])
            if m:
                url = f"{CLERK_BASE_URL}/doc/{m.group(1)}"
        out.append({
            "instrument_number": doc_num,
            "recording_date": normalize_date(cell(COL.COL_DATE)),
            "grantor": cell(COL.COL_GRANTOR),
            "grantee": cell(COL.COL_GRANTEE),
            "doc_type": cell(COL.COL_DOCTYPE),
            "legal": cell(COL.COL_LEGAL),
            "prop_city": cell(COL.COL_TOWN),
            "clerk_url": url,
        })
    return out


def await_ready(page, label: str) -> None:
    try:
        page.evaluate("() => { window.__xcWait = undefined; }")
    except Exception:
        pass
    try:
        page.wait_for_function(READY_JS, timeout=45000)
    except Exception:
        log.warning("  wait timed out [%s]", label)


def fresh_search(browser, url_builder, label: str, max_pages: int = MAX_PAGES) -> list[dict]:
    """One search in one fresh context with homepage warmup (portal serves only
    the first search per session — same constraint fetch.py handles)."""
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    rows: list[dict] = []
    try:
        page = ctx.new_page()
        try:
            page.goto(CLERK_BASE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        time.sleep(1.0)
        offset = 0
        for _ in range(max_pages):
            page.goto(url_builder(offset), wait_until="networkidle", timeout=30000)
            await_ready(page, f"{label} offset={offset}")
            batch = parse_rows(page.content())
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(0.6)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return rows


def confirm_code(rows: list[dict]) -> bool:
    """A candidate code counts only if its rows really are trustee DEEDS."""
    if not rows:
        return False
    hits = sum(1 for r in rows if DEED_RE.search(r["doc_type"]) and not APPT_RE.search(r["doc_type"]))
    return hits > 0 and (hits / len(rows)) > 0.6  # strict majority must be actual trustee deeds


def discover_codes(browser, start, end) -> dict[str, list[dict]]:
    cached: list[str] = []
    if CODE_CACHE.exists():
        try:
            cached = json.loads(CODE_CACHE.read_text()).get("trustee_deed_codes", [])
        except Exception:
            cached = []
    confirmed: dict[str, list[dict]] = {}
    for code in list(dict.fromkeys(cached + CANDIDATE_CODES)):
        rows = fresh_search(browser, lambda off, c=code: build_url(c, start, end, off),
                            f"probe {code}", max_pages=1 if code not in cached else MAX_PAGES)
        if confirm_code(rows):
            log.info("confirmed trustee-deed code %s (%d rows, e.g. %r)",
                     code, len(rows), rows[0]["doc_type"])
            if code not in cached:  # full pull for a newly confirmed code
                rows = fresh_search(browser, lambda off, c=code: build_url(c, start, end, off),
                                    f"full {code}")
            confirmed[code] = rows
        else:
            log.info("candidate %s rejected (%d rows%s)", code, len(rows),
                     f", e.g. {rows[0]['doc_type']!r}" if rows else "")
    CODE_CACHE.write_text(json.dumps(
        {"trustee_deed_codes": sorted(confirmed), "checked_at": datetime.utcnow().isoformat()}, indent=1))
    return confirmed


def enrich_detail(browser, rows: list[dict]) -> None:
    """Pull consideration (winning bid) from the deed detail page where stated."""
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    try:
        page = ctx.new_page()
        for r in rows[:MAX_DETAIL_VISITS]:
            if not r["clerk_url"]:
                continue
            try:
                page.goto(r["clerk_url"], wait_until="networkidle", timeout=30000)
                time.sleep(0.5)
                body = page.evaluate("() => (document.body && document.body.innerText) || ''")
            except Exception as exc:
                log.warning("  detail failed %s: %s", r["instrument_number"], exc)
                continue
            m = CONSIDERATION_RE.search(body)
            if m:
                r["consideration"] = m.group(1).replace(",", "")
            time.sleep(0.6)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def run_county(county: str, base_url: str, start: datetime, end: datetime) -> tuple[list[dict], dict]:
    """Point the module at one GovOS county and pull its deeds."""
    global CLERK_BASE_URL, CLERK_RESULTS, CODE_CACHE
    CLERK_BASE_URL = base_url
    CLERK_RESULTS = f"{base_url}/results"
    CODE_CACHE = DATA / f"doc_code_cache_{county}.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        confirmed = discover_codes(browser, start, end)
        all_rows: dict[str, dict] = {}
        for code, rows in confirmed.items():
            for r in rows:
                if DEED_RE.search(r["doc_type"]) and not APPT_RE.search(r["doc_type"]):
                    r.setdefault("sale_date", "")
                    r.setdefault("beneficiary", "")
                    r.setdefault("consideration", "")
                    r.setdefault("property_address", "")
                    r.setdefault("parcel_id", "")
                    r["county"] = county
                    all_rows[r["instrument_number"]] = r
        rows = sorted(all_rows.values(), key=lambda r: r["recording_date"], reverse=True)
        if rows:
            enrich_detail(browser, rows)
        browser.close()

    per_county = DATA / f"trustee_deeds_{county}.csv"
    with per_county.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + ["county"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    stats = {
        "confirmed_codes": sorted(confirmed),
        "deeds": len(rows),
        "with_consideration": sum(1 for r in rows if r.get("consideration")),
        "warning": None if confirmed else (
            f"{county}: NO trustee-deed doc code confirmed — set TRUSTEE_DEED_CODES "
            "after checking that portal's document-type filter manually."),
    }
    return rows, stats


def main() -> None:
    DATA.mkdir(exist_ok=True)
    end = datetime.utcnow() - timedelta(days=CERT_LAG_DAYS)
    start = end - timedelta(days=LOOKBACK_DAYS)
    log.info("trustee deeds window %s .. %s", start.date(), end.date())

    wanted = [c.strip().lower() for c in os.environ.get(
        "DEED_COUNTIES", ",".join(GOVOS_COUNTIES)).split(",") if c.strip()]
    combined: list[dict] = []
    per_county_stats: dict[str, dict] = {}
    for county in wanted:
        base = GOVOS_COUNTIES.get(county)
        if not base:
            log.warning("county %s is not a configured GovOS county — skipping", county)
            continue
        log.info("=== %s county ===", county)
        try:
            rows, stats = run_county(county, base, start, end)
        except Exception as exc:  # one county failing must not kill the others
            log.error("%s county failed: %s", county, exc)
            rows, stats = [], {"error": str(exc), "deeds": 0, "with_consideration": 0,
                               "confirmed_codes": []}
        combined.extend(rows)
        per_county_stats[county] = stats

    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + ["county"], extrasaction="ignore")
        w.writeheader()
        w.writerows(combined)

    summary = {
        "generated_at": datetime.utcnow().isoformat(),
        "window": [start.date().isoformat(), end.date().isoformat()],
        "counties": per_county_stats,
        "deeds": len(combined),
        "with_consideration": sum(1 for r in combined if r.get("consideration")),
    }
    SUMMARY.write_text(json.dumps(summary, indent=1))
    log.info("trustee_deeds.csv: %d deeds (%d with consideration) across %s",
             summary["deeds"], summary["with_consideration"], list(per_county_stats))


if __name__ == "__main__":
    main()
