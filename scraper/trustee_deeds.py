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
    "tarrant": "https://tarrant.tx.publicsearch.us",
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

# Per-county candidates and deed-description tests. Bexar's doc table (checked 2026-10-18)
# has NO "TRUSTEE ... DEED" type; foreclosure deeds there record under FC = "FORECLOSURE".
COUNTY_CANDIDATES = {
    "dallas": ["TD", "TR DEED"],
    "bexar": ["FC", "TD", "TR DEED"],
    "tarrant": [],           # rely on homepage doc-table discovery
}
COUNTY_DEED_DESC = {
    "bexar": re.compile(r"^FORECLOSURE\b|TRUSTEE.{0,12}DEED|DEED.{0,12}TRUSTEE", re.IGNORECASE),
}

DEED_RE = re.compile(r"TRUSTEE.{0,12}DEED|DEED.{0,12}TRUSTEE", re.IGNORECASE)
APPT_RE = re.compile(r"APPOINT", re.IGNORECASE)
CONSIDERATION_RE = re.compile(
    r"(?:CONSIDERATION|SALES?\s+PRICE|AMOUNT)\s*[:\-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE)

# --- OCR patterns for the deed's own text (substitute trustee's deed boilerplate) ---
OCR_BID_RE = re.compile(
    r"(?:sum|amount|consideration|bid(?:der)?|paid?|price|cash)\s*(?:of|in|for|:)?[^$\n]{0,80}\$\s*([\d,]{4,}(?:\.\d{2})?)",
    re.IGNORECASE)
OCR_BID_FALLBACK_RE = re.compile(r"\$\s*([\d,]{5,}(?:\.\d{2})?)")
OCR_PRINCIPAL_RE = re.compile(
    r"original\s+principal\s+(?:amount|sum|balance)?[^$]{0,60}\$\s*([\d,]{4,}(?:\.\d{2})?)",
    re.IGNORECASE)
OCR_MORTGAGOR_RE = re.compile(
    r"(?:Mortgagors?|Grantors?\s*\(Mortgagors?\)|executed\s+by)\s*[:\-]?\s*"
    r"([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,4}(?:\s+(?:AND|and|&)\s+[A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,3})?)")
OCR_SALEDATE_RE = re.compile(
    r"(?:date\s+of\s+sale|sale\s+(?:was\s+)?(?:held|conducted)\s+on|sold\s+on)\s*[:\-]?\s*"
    r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)

OCR_ADDRESS_RE = re.compile(
    r"(?:commonly\s+known\s+as|property\s+address)[:\s]+(\d{1,6}[^,\n]{4,60}(?:,\s*[^,\n]{2,40}){0,2})",
    re.IGNORECASE)

FIELDS = ["instrument_number", "recording_date", "sale_date", "grantor", "grantee",
          "beneficiary", "consideration", "original_principal", "mortgagors",
          "owner_full", "property_address", "mail_address", "assessed_value",
          "parcel_source", "parcel_id", "doc_type", "legal", "clerk_url", "ocr_used"]

# --- County appraisal-district parcel APIs (ArcGIS). Only configured, verified entries
# are queried; counties without one are reported in the summary, never guessed. ---
PARCEL_APIS = {
    "dallas": {
        "url": "https://maps.dcad.org/prdwa/rest/services/Property/ParcelQuery/MapServer/4/query",
        "owner": "OWNERNME1", "owner2": "OWNERNME2", "addr": "SITEADDRESS",
        "subdiv": "CNVYNAME", "legal": "PRPRTYDSCRP",
        "mail": ("PSTLADDRESS", "PSTLCITY", "PSTLSTATE", "PSTLZIP5"),
        "value": "CNTASSDVAL",
    },
    # bexar / tarrant: BCAD & TAD endpoints to be verified before adding — do not guess.
}
TRUSTEE_GRANTOR_RE = re.compile(r"\b(TR(USTEE)?S?|SUBSTITUTE|LLC|INC|CORP|BANK|AUCTION|LAW|PLLC)\b", re.IGNORECASE)
ORDINALS = {"ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5", "SIX": "6",
            "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10"}

# Reuse the exact grid column map + ready JS from the proven scraper
_SC = fetch.ClerkScraper if hasattr(fetch, "ClerkScraper") else None
if _SC is None:  # fall back to whatever the class is named
    _SC = next(getattr(fetch, n) for n in dir(fetch)
               if n.endswith("Scraper") and hasattr(getattr(fetch, n), "COL_DOCTYPE"))
COL = _SC
READY_JS = _SC._READY_JS
UA = _SC._UA


COUNTY_URL_STYLE = {"bexar": "advanced"}   # bexar portal honors advancedSearch&docTypes=


def build_url(doc_code: str, start: datetime, end: datetime, offset: int = 0) -> str:
    params = {
        "department": "RP",
        "keywordSearch": "false", "searchOcrText": "false",
        "recordedDateRange": f"{start.strftime('%Y%m%d')},{end.strftime('%Y%m%d')}",
        "limit": PAGE_SIZE, "offset": offset,
    }
    if COUNTY_URL_STYLE.get(CURRENT_COUNTY) == "advanced":
        params["searchType"] = "advancedSearch"
        params["docTypes"] = doc_code
    else:
        params["searchType"] = "quickSearch"
        params["_docTypes"] = doc_code
    return f"{CLERK_RESULTS}?{urlencode(params)}"


HEADER_MAP = [
    ("grantor", re.compile(r"GRANTOR", re.I)),
    ("grantee", re.compile(r"GRANTEE", re.I)),
    ("doc_type", re.compile(r"DOC(UMENT)?\s*TYPE", re.I)),
    ("recording_date", re.compile(r"RECORDED|RECORD\s*DATE|FILE[DS]?\s*DATE", re.I)),
    ("instrument_number", re.compile(r"DOC(UMENT)?\s*(#|NO|NUM)|INSTRUMENT", re.I)),
    ("prop_city", re.compile(r"TOWN|CITY", re.I)),
    ("legal", re.compile(r"LEGAL", re.I)),
    ("property_address", re.compile(r"ADDRESS", re.I)),
]


def _column_map(table) -> dict:
    """Map column indexes by header text — GovOS grids differ per county."""
    header = table.find("tr")
    cols = {}
    if header:
        for i, th in enumerate(header.find_all(["th", "td"])):
            text = th.get_text(strip=True)
            for key, pat in HEADER_MAP:
                if key not in cols and pat.search(text):
                    cols[key] = i
    # fall back to the proven Dallas layout for anything unmapped
    defaults = {"grantor": COL.COL_GRANTOR, "grantee": COL.COL_GRANTEE,
                "doc_type": COL.COL_DOCTYPE, "recording_date": COL.COL_DATE,
                "instrument_number": COL.COL_DOCNUM, "prop_city": COL.COL_TOWN,
                "legal": COL.COL_LEGAL}
    for k, v in defaults.items():
        cols.setdefault(k, v)
    return cols


def parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []
    cols = _column_map(table)
    out = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        def cell(key):
            i = cols.get(key, -1)
            return cells[i].get_text(strip=True) if 0 <= i < len(cells) else ""
        doc_num = cell("instrument_number")
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
            "recording_date": normalize_date(cell("recording_date")),
            "grantor": cell("grantor"),
            "grantee": cell("grantee"),
            "doc_type": cell("doc_type"),
            "legal": cell("legal"),
            "prop_city": cell("prop_city"),
            "property_address": cell("property_address"),
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


CURRENT_COUNTY = "dallas"


def _deed_desc_re():
    return COUNTY_DEED_DESC.get(CURRENT_COUNTY, DEED_RE)


def _is_deed(doc_type: str) -> bool:
    return bool(_deed_desc_re().search(doc_type or "")) and not APPT_RE.search(doc_type or "")


def confirm_code(rows: list[dict]) -> bool:
    """A candidate code counts only if its rows really are foreclosure-sale DEEDS."""
    if not rows:
        return False
    hits = sum(1 for r in rows if _is_deed(r["doc_type"]))
    return hits > 0 and (hits / len(rows)) > 0.6  # strict majority must be actual deeds


DOC_TABLE_RE = re.compile(r'\{"code":"([^"]{1,12})","description":"([^"]*)"\}')


def codes_from_homepage(browser) -> list[str]:
    """Each GovOS county embeds its own doc-type table in the homepage HTML.
    Pull codes whose description reads TRUSTEE...DEED (not APPOINTMENT)."""
    ctx = browser.new_context(user_agent=UA)
    found: list[str] = []
    try:
        page = ctx.new_page()
        page.goto(CLERK_BASE_URL, wait_until="domcontentloaded", timeout=30000)
        html = page.content()
        for code, desc in DOC_TABLE_RE.findall(html):
            d = desc.replace("\\u002F", "/").replace("\\/", "/")
            if _is_deed(d) or (COUNTY_DEED_DESC.get(CURRENT_COUNTY) and COUNTY_DEED_DESC[CURRENT_COUNTY].search(d)):
                found.append(code)
    except Exception as exc:
        log.warning("homepage doc-table read failed: %s", exc)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    if found:
        log.info("doc-table trustee-deed codes: %s", found)
    return found


def discover_codes(browser, start, end) -> dict[str, list[dict]]:
    cached: list[str] = []
    if CODE_CACHE.exists():
        try:
            cached = json.loads(CODE_CACHE.read_text()).get("trustee_deed_codes", [])
        except Exception:
            cached = []
    table_codes = codes_from_homepage(browser)   # county's own table, highest signal
    confirmed: dict[str, list[dict]] = {}
    county_cands = COUNTY_CANDIDATES.get(CURRENT_COUNTY, [])
    for code in list(dict.fromkeys(cached + table_codes + county_cands + CANDIDATE_CODES)):
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


def _ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


_GRAB_PREVIEW_JS = """async () => {
  // Largest rendered image or canvas on the doc detail page = the deed preview.
  const imgs = [...document.querySelectorAll('img')]
    .filter(i => i.naturalWidth > 400 && i.naturalHeight > 400)
    .sort((a, b) => b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight);
  const cvs = [...document.querySelectorAll('canvas')]
    .filter(c => c.width > 400 && c.height > 400)
    .sort((a, b) => b.width * b.height - a.width * a.height);
  try {
    if (cvs.length) return cvs[0].toDataURL('image/png');
  } catch (e) { /* tainted canvas -> fall through to img */ }
  if (imgs.length) {
    try {
      const resp = await fetch(imgs[0].src);
      const blob = await resp.blob();
      return await new Promise(res => { const fr = new FileReader();
        fr.onload = () => res(fr.result); fr.readAsDataURL(blob); });
    } catch (e) { return null; }
  }
  return null;
}"""


def _ocr_deed_text(data_url: str) -> str:
    import base64
    import io
    import pytesseract
    from PIL import Image
    raw = base64.b64decode(data_url.split(",", 1)[1])
    img = Image.open(io.BytesIO(raw)).convert("L")
    if img.width < 1400:  # upscale for OCR accuracy on small previews
        img = img.resize((img.width * 2, img.height * 2))
    return pytesseract.image_to_string(img)


def _money_str(m: re.Match | None) -> str:
    return m.group(1).replace(",", "") if m else ""


def enrich_detail(browser, rows: list[dict]) -> dict:
    """Per deed: read detail-page text, and OCR the deed preview image for the figures the
    deed itself states (winning bid, original principal, mortgagors, sale date).
    Fields stay blank when not found — blank means UNKNOWN downstream, never $0."""
    ocr_ok = _ocr_available()
    stats = {"visited": 0, "ocr_ran": 0, "got_price": 0, "got_principal": 0, "got_mortgagor": 0}
    if not ocr_ok:
        log.warning("pytesseract/Pillow not installed — detail text only, no deed OCR")
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1000})
    try:
        page = ctx.new_page()
        captured: list = []

        def _on_response(resp):
            # Deed page images stream as signed PNGs: /files/documents/<id>/images/<page>_1.png
            if re.search(r"/files/documents/\d+/images/[^/]+\.(png|jpe?g)", resp.url) and resp.ok:
                try:
                    if len(captured) < 3:
                        captured.append(resp.body())
                except Exception:
                    pass

        page.on("response", _on_response)
        samples_dir = DATA / "ocr_samples"
        samples_dir.mkdir(exist_ok=True)
        samples_saved = 0
        for r in rows[:MAX_DETAIL_VISITS]:
            if not r["clerk_url"]:
                continue
            del captured[:]
            try:
                page.goto(r["clerk_url"], wait_until="networkidle", timeout=30000)
                time.sleep(1.2)
                # advance the viewer to pull pages 2-3 (bid/principal often sit past the cover page)
                for _ in range(2):
                    try:
                        page.keyboard.press("ArrowRight")
                        time.sleep(1.0)
                    except Exception:
                        break
                body = page.evaluate("() => (document.body && document.body.innerText) || ''")
            except Exception as exc:
                log.warning("  detail failed %s: %s", r["instrument_number"], exc)
                continue
            stats["visited"] += 1
            m = CONSIDERATION_RE.search(body)
            if m:
                r["consideration"] = m.group(1).replace(",", "")

            if ocr_ok and not (r.get("consideration") and r.get("original_principal")):
                import base64 as _b64
                data_urls = ["data:image/png;base64," + _b64.b64encode(b).decode() for b in captured]
                if not data_urls:
                    try:
                        dom_grab = page.evaluate(_GRAB_PREVIEW_JS)
                        if dom_grab:
                            data_urls = [dom_grab]
                    except Exception:
                        pass
                if data_urls:
                    try:
                        text = "\n".join(_ocr_deed_text(u) for u in data_urls)
                        if samples_saved < 3 and text.strip():
                            (samples_dir / f"{r['county']}_{r['instrument_number']}.txt").write_text(text[:8000])
                            samples_saved += 1
                        stats["ocr_ran"] += 1
                        if not r.get("consideration"):
                            r["consideration"] = _money_str(OCR_BID_RE.search(text))
                        if not r.get("consideration"):
                            # fallback: the largest dollar figure on the deed that is not
                            # the original principal (bids are the big number on these docs)
                            principal = _money_str(OCR_PRINCIPAL_RE.search(text))
                            amts = [a.replace(",", "") for a in OCR_BID_FALLBACK_RE.findall(text)]
                            amts = [a for a in amts if a != principal and float(a) >= 10000]
                            if amts:
                                r["consideration"] = max(amts, key=float)
                        r["original_principal"] = _money_str(OCR_PRINCIPAL_RE.search(text))
                        mm = OCR_MORTGAGOR_RE.search(text)
                        if mm:
                            r["mortgagors"] = re.sub(r"\s+", " ", mm.group(1)).strip()
                        sd = OCR_SALEDATE_RE.search(text)
                        if sd and not r.get("sale_date"):
                            r["sale_date"] = normalize_date(sd.group(1).replace(",", " ").strip())
                        ad = OCR_ADDRESS_RE.search(text)
                        if ad and not r.get("property_address"):
                            addr = re.split(r"\s+(?:together|being|and\s+all)\b",
                                            ad.group(1), 1)[0]
                            r["property_address"] = re.sub(r"\s+", " ", addr).strip(" ,")
                        r["ocr_used"] = "1"
                    except Exception as exc:
                        log.warning("  OCR failed %s: %s", r["instrument_number"], exc)
            stats["got_price"] += bool(r.get("consideration"))
            stats["got_principal"] += bool(r.get("original_principal"))
            stats["got_mortgagor"] += bool(r.get("mortgagors"))
            time.sleep(0.6)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    log.info("detail enrichment: %s", stats)
    return stats


LEGAL_RE = re.compile(r"Name:\s*(?P<name>.+?)\s+(?:Lot:\s*(?P<lot>[\w-]+))?\s*(?:Block:\s*(?P<blk>[\w-]+))?\s*(?:,|$)")
TOWN_RE = re.compile(r"Town:\s*([A-Z &.-]+?)\s+Name:", re.IGNORECASE)


def _subdiv_tokens(name: str) -> list:
    """'WOODBROOK INSTALLMENT NO TWO' -> ['WOODBROOK', '2'] (DCAD abbreviates)."""
    drop = {"INSTALLMENT", "INSTL", "NO", "PHASE", "PH", "SECTION", "SEC", "ADDITION", "ADDN",
            "REVISED", "REV", "REPLAT", "ESTATES", "ESTATE", "THE", "OF", "AT", "ON"}
    out = []
    for t in re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split():
        if t in ORDINALS:
            out.append(ORDINALS[t])
        elif t not in drop:
            out.append(t)
    return out


def _arcgis_query(api: dict, where: str, n: int = 5) -> list:
    import requests
    fields = [api["owner"], api["owner2"], api["addr"], api["subdiv"], api["legal"],
              api["value"], *api["mail"]]
    try:
        r = requests.get(api["url"], params={
            "where": where, "outFields": ",".join(fields), "returnGeometry": "false",
            "f": "json", "resultRecordCount": n}, timeout=25,
            headers={"User-Agent": "DallasLeadScraper/1.0"})
        return [f["attributes"] for f in r.json().get("features", [])]
    except Exception as exc:
        log.warning("  parcel query failed: %s", exc)
        return []


def _fill_from_parcel(r: dict, hit: dict, api: dict, source: str) -> None:
    owner = " ".join(x for x in [hit.get(api["owner"]), hit.get(api["owner2"])] if x).strip()
    r["owner_full"] = re.sub(r"\s+", " ", owner)
    site = (hit.get(api["addr"]) or "").strip()
    town = (TOWN_RE.search(r.get("legal") or "") or [None, ""])[1] if TOWN_RE.search(r.get("legal") or "") else ""
    if site:
        r["property_address"] = f"{site}, {town.title()}, TX".replace(", , ", ", ") if town else f"{site}, TX"
    ma, mc, ms, mz = (str(hit.get(k) or "").strip() for k in api["mail"])
    if ma:
        r["mail_address"] = re.sub(r"\s+", " ", f"{ma}, {mc}, {ms} {mz}")
    if hit.get(api["value"]) is not None:
        r["assessed_value"] = str(hit[api["value"]])
    r["parcel_source"] = source


def enrich_parcels(rows: list) -> dict:
    """Cross-reference each deed against the county appraisal district:
    owner-name lookup first (tax roll still shows the foreclosed owner), legal-description
    fallback accepted only on a single unambiguous hit. Blank stays blank."""
    sys.path  # keep import local pattern obvious
    from fetch import _sql_lit, normalize_owner_for_parcel
    stats = {"api_missing": set(), "owner_hits": 0, "legal_hits": 0, "unmatched": 0}
    for r in rows:
        api = PARCEL_APIS.get(r.get("county", "dallas"))
        if not api:
            stats["api_missing"].add(r.get("county"))
            continue
        legal = r.get("legal") or ""
        m = LEGAL_RE.search(legal)
        sub_tokens = _subdiv_tokens(m.group("name")) if m else []

        # 1) owner-name path: OCR mortgagor, else a person-looking grantor from the grid
        candidates = []
        if r.get("mortgagors"):
            candidates.append(r["mortgagors"])
        g = (r.get("grantor") or "").strip()
        if g and not TRUSTEE_GRANTOR_RE.search(g):
            candidates.append(g)
        hit = None
        name_variants = []
        for cand in candidates:
            norm = normalize_owner_for_parcel(cand)
            if len(norm) >= 5:
                name_variants.append(norm)
                parts = norm.split()
                if len(parts) == 2:                      # OCR order FIRST LAST → try LAST FIRST
                    name_variants.append(f"{parts[1]} {parts[0]}")
        for norm in name_variants:
            found = _arcgis_query(api, f"UPPER({api['owner']}) LIKE '{_sql_lit(norm)}%'")
            if len(found) == 1:
                hit = (found[0], "owner")
            elif len(found) > 1 and sub_tokens:
                # disambiguate by subdivision tokens
                scored = [f for f in found
                          if all(t in (f.get(api["subdiv"]) or "").upper() for t in sub_tokens[:2])]
                if len(scored) == 1:
                    hit = (scored[0], "owner+legal")
            if hit:
                break

        # 2) legal fallback: single unambiguous parcel only
        if not hit and m and sub_tokens:
            parts = [f"UPPER({api['subdiv']}) LIKE '{_sql_lit(' '.join(sub_tokens[:2]))}%'"]
            if m.group("lot"):
                parts.append(f"{api['legal']} LIKE '%LT {_sql_lit(m.group('lot'))}%'")
            if m.group("blk"):
                parts.append(f"{api['legal']} LIKE '%BLK%{_sql_lit(m.group('blk'))}%'")
            found = _arcgis_query(api, " AND ".join(parts), n=3)
            if len(found) == 1:
                hit = (found[0], "legal")

        if hit:
            _fill_from_parcel(r, hit[0], api, f"dcad:{hit[1]}")
            stats["owner_hits" if "owner" in hit[1] else "legal_hits"] += 1
        else:
            stats["unmatched"] += 1
        time.sleep(0.3)
    stats["api_missing"] = sorted(x for x in stats["api_missing"] if x)
    log.info("parcel enrichment: %s", stats)
    return stats


def run_county(county: str, base_url: str, start: datetime, end: datetime) -> tuple[list[dict], dict]:
    """Point the module at one GovOS county and pull its deeds."""
    global CLERK_BASE_URL, CLERK_RESULTS, CODE_CACHE, CURRENT_COUNTY
    CURRENT_COUNTY = county
    CLERK_BASE_URL = base_url
    CLERK_RESULTS = f"{base_url}/results"
    CODE_CACHE = DATA / f"doc_code_cache_{county}.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        confirmed = discover_codes(browser, start, end)
        all_rows: dict[str, dict] = {}
        for code, rows in confirmed.items():
            for r in rows:
                if _is_deed(r["doc_type"]):
                    for k in ("sale_date", "beneficiary", "consideration", "original_principal",
                              "mortgagors", "owner_full", "property_address", "mail_address",
                              "assessed_value", "parcel_source", "parcel_id", "ocr_used"):
                        r.setdefault(k, "")
                    r["county"] = county
                    all_rows[r["instrument_number"]] = r
        rows = sorted(all_rows.values(), key=lambda r: r["recording_date"], reverse=True)
        enrich_stats = enrich_detail(browser, rows) if rows else {}
        browser.close()
    parcel_stats = enrich_parcels(rows) if rows else {}

    per_county = DATA / f"trustee_deeds_{county}.csv"
    with per_county.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + ["county"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    stats = {
        "confirmed_codes": sorted(confirmed),
        "deeds": len(rows),
        "with_consideration": sum(1 for r in rows if r.get("consideration")),
        "with_principal": sum(1 for r in rows if r.get("original_principal")),
        "with_mortgagor": sum(1 for r in rows if r.get("mortgagors")),
        "with_owner": sum(1 for r in rows if r.get("owner_full")),
        "with_address": sum(1 for r in rows if r.get("property_address")),
        "enrichment": enrich_stats,
        "parcels": parcel_stats,
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
