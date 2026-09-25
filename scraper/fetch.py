#!/usr/bin/env python3
"""
Dallas County (Texas) Motivated Seller Lead Scraper
====================================================
Uses Playwright to render the GovOS clerk portal (React SPA, server-rendered
tables), scrapes Real Property doc-type searches plus the Foreclosures
department (upcoming trustee-sale notices), enriches with DCAD parcel data,
scores leads, exports JSON + GHL CSV + skip-trace CSV.

Clerk Portal : https://dallas.tx.publicsearch.us/
  - department=RP  (Property Records)  filter: _docTypes=<CODE>
  - department=FC  (Foreclosures)      dates:  instrumentDateRange (SALE date)
Parcel Data  : https://maps.dcad.org/prdwa/rest/services/Property/ParcelQuery/MapServer/4
  - OWNERNME1 format "LAST FIRST M [& SPOUSE]", SITEADDRESS street-only,
    mailing in PSTLADDRESS/PSTLCITY/PSTLSTATE/PSTLZIP5 (trailing spaces)

Dallas portal gotchas (learned live):
  - RP result grid has NO property-address column; columns are
    [3]=Grantor [4]=Grantee [5]=DocType [6]=RecordedDate [7]=DocNumber
    [8]=Book/Vol/Page [9]=Town [10]=LegalDescription
  - FC grid: [3]=DocType [4]=RecordedDate [5]=SaleDate [6]=DocNumber
    [7]=PropertyAddress (CITY ONLY; street is inside the scanned PDF)
  - Queries whose recordedDateRange extends past the "Certified through"
    date intermittently return an empty result set: clamp end to
    (today - CERT_LAG_DAYS) and, if the whole RP pass returns zero,
    retry once with a further 4-day clamp.
  - advancedSearch+docTypes= (the Bexar pattern) is NOT honored here;
    quickSearch + _docTypes=<CODE> is what the portal itself produces.
  - The portal reliably serves only the FIRST search of a browser
    session; later deep-linked searches in the same session come back
    "No Results" (proven on run #1: LP=10 then all zeros). Each search
    therefore runs in a FRESH browser context with its own homepage
    warmup; pagination offsets within one search stay in that context.

Run:
    python scraper/fetch.py                # default 7-day lookback
    python scraper/fetch.py --days 14
    python scraper/fetch.py --skip-parcel
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COUNTY = "Dallas"
STATE = "TX"
CLERK_BASE_URL = "https://dallas.tx.publicsearch.us"
CLERK_RESULTS = f"{CLERK_BASE_URL}/results"
PARCEL_API_URL = ("https://maps.dcad.org/prdwa/rest/services/Property/"
                  "ParcelQuery/MapServer/4/query")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
PROBATE_LOOKBACK_DAYS = 60   # probate/heirship filings move slow
CERT_LAG_DAYS = 3            # portal certification lags ~3-4 days
FC_HORIZON_DAYS = 95         # upcoming trustee sales window (sale date)
PAGE_SIZE = 50
REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_DELAY = 3
ARCGIS_MAX_LOOKUPS = 1500    # cap per-record DCAD owner/address lookups

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dallas_scraper")

# ---------------------------------------------------------------------------
# Real Property doc-type codes -> (cat, label)
# Codes verified against the portal's docTypeLookupTable (Sept 2026) and the
# sidebar filter (_docTypes=<CODE>). 3-week volumes in comments.
# ---------------------------------------------------------------------------
DOC_TYPES = [
    ("LP",   "LP",      "Lis Pendens"),                    # 36 /3wk
    ("AT",   "FC",      "Appointment of Substitute Trustee"),  # ~6/wk
    ("AJ",   "JUD",     "Abstract of Judgment"),           # 413 /3wk
    ("JUD",  "JUD",     "Judgment"),                       # 12 /3wk
    ("STL",  "LIEN",    "State Tax Lien"),
    ("FTL",  "LIEN",    "Federal Tax Lien"),               # 134 /3wk
    ("FTLE", "LIEN",    "Federal Tax Lien Notice"),
    ("HL",   "LIEN",    "Hospital Lien"),                  # 395 /3wk
    ("CSL",  "LIEN",    "Child Support Lien"),             # 6 /3wk
    ("ML",   "LIEN",    "Mechanic's Lien"),                # 24 /3wk
    ("ALN",  "LIEN",    "HOA Assessment Lien"),            # 140 /3wk
    ("TXD",  "TAXDEED", "Tax Deed"),
    ("TXS",  "TAXDEED", "Tax Sale"),
    ("AH",   "PRO",     "Affidavit of Heirship"),          # 304 /3wk
    ("AHC",  "PRO",     "Affidavit of Heirship & Conveyance"),
    ("PB",   "PRO",     "Probate Proceedings"),            # 43 /3wk
]

# Doc-type codes that move slowly and use the wider probate lookback window.
PROBATE_CODES = {"AH", "AHC", "PB"}
PROBATE_CATS = {"PRO"}

MAX_PAGES_PER_DOC_TYPE = 80

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
GHL_FIELDS = [
    "doc_num","doc_type","cat","cat_label","filed","owner","grantee",
    "amount","prop_address","prop_city","prop_state","prop_zip",
    "mail_address","mail_city","mail_state","mail_zip","legal","clerk_url","score","flags",
    "first_seen","status",
]
GHL_HEADERS = {f: f.replace("_", " ").title() for f in GHL_FIELDS}
GHL_HEADERS["first_seen"] = "Date Entered System"
GHL_HEADERS["status"] = "Status"

@dataclass
class LeadRecord:
    doc_num: str = ""
    doc_type: str = ""
    cat: str = ""
    cat_label: str = ""
    filed: str = ""
    owner: str = ""
    grantee: str = ""
    amount: float = 0.0
    legal: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = STATE
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = STATE
    mail_zip: str = ""
    clerk_url: str = ""
    flags: list = field(default_factory=list)
    score: int = 0
    status: str = ""
    first_seen: str = ""
    rid: str = ""
    content_hash: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw.strip()


def _norm_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def _arc_val(x) -> str:
    s = _norm_ws(x)
    return "" if s.upper() in ("NULL", "NONE") else s


def _sql_lit(s: str) -> str:
    return s.upper().replace("'", "''")


def normalize_owner_for_parcel(name: str) -> str:
    """DCAD OWNERNME1 is 'LAST FIRST M [& SPOUSE]' uppercase.

    Clerk records give 'LAST, FIRST M', 'LAST FIRST', or entity names.
    Normalize to 'LAST FIRST' (two tokens) for a forward LIKE prefix.
    """
    if not name:
        return ""
    n = _norm_ws(name).upper()
    n = re.sub(r"\b(JR|SR|II|III|IV)\.?$", "", n).strip().rstrip(",")
    if "," in n:
        last, first = n.split(",", 1)
        first_tok = first.strip().split()
        return f"{last.strip()} {first_tok[0] if first_tok else ''}".strip()
    parts = n.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return n

# ---------------------------------------------------------------------------
# Clerk portal scraper - Playwright
# ---------------------------------------------------------------------------
class ClerkScraper:
    """
    Playwright (headless Chromium) against the GovOS portal.

    RP grid columns (live inspection 2026-09-25):
        [0..2]=controls [3]=Grantor [4]=Grantee [5]=Doc Type
        [6]=Recorded Date [7]=Doc Number [8]=Book/Volume/Page
        [9]=Town [10]=Legal Description        (NO address column)
    FC grid columns:
        [0..2]=controls [3]=Doc Type [4]=Recorded Date [5]=Sale Date
        [6]=Doc Number [7]=Property Address (city only)
    Deep links from row checkbox id="table-checkbox-<docId>" -> /doc/<id>.
    """
    # RP columns
    COL_GRANTOR = 3
    COL_GRANTEE = 4
    COL_DOCTYPE = 5
    COL_DATE = 6
    COL_DOCNUM = 7
    COL_TOWN = 9
    COL_LEGAL = 10
    # FC columns
    FC_DOCTYPE = 3
    FC_RECDATE = 4
    FC_SALEDATE = 5
    FC_DOCNUM = 6
    FC_CITY = 7

    def __init__(self, default_start: datetime, default_end: datetime,
                 probate_start: datetime, fc_end: datetime):
        self.default_start = default_start
        self.default_end = default_end
        self.probate_start = probate_start
        self.fc_end = fc_end

    def _date_range(self, doc_code: str, clamp_extra_days: int = 0) -> str:
        end = self.default_end - timedelta(days=clamp_extra_days)
        start = self.probate_start if doc_code in PROBATE_CODES else self.default_start
        return f"{start.strftime('%Y%m%d')},{end.strftime('%Y%m%d')}"

    def _build_url(self, doc_code: str, offset: int = 0,
                   clamp_extra_days: int = 0) -> str:
        from urllib.parse import urlencode
        params = {
            "department": "RP",
            "searchType": "quickSearch",
            "keywordSearch": "false",
            "searchOcrText": "false",
            "_docTypes": doc_code,
            "recordedDateRange": self._date_range(doc_code, clamp_extra_days),
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        return f"{CLERK_RESULTS}?{urlencode(params)}"

    def _build_fc_url(self, offset: int = 0) -> str:
        from urllib.parse import urlencode
        params = {
            "department": "FC",
            "searchType": "quickSearch",
            "keywordSearch": "false",
            "searchOcrText": "false",
            # FC dates are SALE dates; window = today .. +FC_HORIZON_DAYS
            "instrumentDateRange": (
                f"{self.default_end.strftime('%Y%m%d')},"
                f"{self.fc_end.strftime('%Y%m%d')}"),
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        return f"{CLERK_RESULTS}?{urlencode(params)}"

    @staticmethod
    def _row_clerk_url(tr) -> str:
        cb = tr.find("input", id=re.compile(r"table-checkbox-(\d+)"))
        if cb and cb.get("id"):
            m = re.search(r"table-checkbox-(\d+)", cb["id"])
            if m:
                return f"{CLERK_BASE_URL}/doc/{m.group(1)}"
        return ""

    def _parse_rp_html(self, html: str, cat: str, cat_label: str) -> list:
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if not table:
            return []
        records = []
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= self.COL_DOCNUM:
                continue
            def cell(idx: int) -> str:
                return cells[idx].get_text(strip=True) if idx < len(cells) else ""
            grantor = cell(self.COL_GRANTOR)
            doc_num = cell(self.COL_DOCNUM)
            if not doc_num and not grantor:
                continue
            town = cell(self.COL_TOWN)
            if town.upper() in ("N/A", "NA", "OTHER", "--"):
                town = ""
            rec = LeadRecord(
                doc_num=doc_num,
                doc_type=cell(self.COL_DOCTYPE),
                cat=cat, cat_label=cat_label,
                filed=normalize_date(cell(self.COL_DATE)) if cell(self.COL_DATE) else "",
                owner=grantor, grantee=cell(self.COL_GRANTEE),
                legal=cell(self.COL_LEGAL),
                prop_city=town.title() if town else "",
                prop_state=STATE,
                clerk_url=self._row_clerk_url(tr),
            )
            records.append(rec)
        return records

    def _parse_fc_html(self, html: str) -> list:
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if not table:
            return []
        records = []
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= self.FC_DOCNUM:
                continue
            def cell(idx: int) -> str:
                return cells[idx].get_text(strip=True) if idx < len(cells) else ""
            doc_num = cell(self.FC_DOCNUM)
            if not doc_num:
                continue
            sale = normalize_date(cell(self.FC_SALEDATE)) if cell(self.FC_SALEDATE) else ""
            city = cell(self.FC_CITY)
            if city.upper() in ("N/A", "NA", "OTHER", "--"):
                city = ""
            rec = LeadRecord(
                doc_num=doc_num,
                doc_type=cell(self.FC_DOCTYPE) or "NOTICE OF FORECLOSURE",
                cat="FC",
                cat_label=(f"Trustee Sale {sale}" if sale
                           else "Foreclosure Notice"),
                filed=normalize_date(cell(self.FC_RECDATE)) if cell(self.FC_RECDATE) else "",
                legal=f"Trustee sale date: {sale}" if sale else "",
                prop_city=city.title() if city else "",
                prop_state=STATE,
                clerk_url=self._row_clerk_url(tr),
            )
            records.append(rec)
        return records

    # JS: resolve true once tbody row count is stable & non-zero >=600ms, or
    # the at-rest empty template (both unique markers) is rendered. The SPA
    # briefly re-renders the previous search's rows during route changes, so
    # a stable non-zero count rejects transient ghost rows.
    _READY_JS = """() => {
        if (!window.__xcWait) { window.__xcWait = { lastCount: -1, since: 0 }; }
        const st = window.__xcWait;
        const now = Date.now();
        const count = document.querySelectorAll('tbody tr[role="row"]').length;
        if (count > 0) {
          if (count === st.lastCount) {
            if (now - st.since >= 600) return true;
          } else { st.lastCount = count; st.since = now; }
          return false;
        }
        const body = (document.body && document.body.innerText) || '';
        if (body.includes('No Results Found') || body.includes('No results found')) return true;
        return false;
    }"""

    _WAIT_TIMEOUT_MS = 45000
    _AMBIGUOUS_RETRIES = 3

    def _await_ready(self, page, label: str, url: str) -> None:
        for attempt in range(1, self._AMBIGUOUS_RETRIES + 1):
            try:
                page.evaluate("() => { window.__xcWait = undefined; }")
            except Exception:
                pass
            try:
                page.wait_for_function(self._READY_JS, timeout=self._WAIT_TIMEOUT_MS)
                return
            except PWTimeout:
                body = ""
                try:
                    body = page.evaluate("() => (document.body && document.body.innerText) || ''")
                except Exception:
                    pass
                if "No Results Found" in body or "No results found" in body:
                    return
                if attempt < self._AMBIGUOUS_RETRIES:
                    log.warning("  ambiguous wait [%s] attempt %d/%d; re-navigating",
                                label, attempt, self._AMBIGUOUS_RETRIES)
                    try:
                        page.goto("about:blank", wait_until="commit", timeout=5000)
                        page.goto(url, wait_until="networkidle", timeout=30000)
                    except Exception:
                        pass
                    continue
                log.info("  no results (timeout) [%s]", label)
                return

    def _fetch_pages(self, page, build_url, parse, label: str,
                     start_offset: int = 0) -> tuple:
        """Paginate one search inside one context.

        Returns (records, stop_offset, natural_end): natural_end is True on
        a partial page or the page cap; False when a page came back empty
        after full pages - which may be the session block, so the caller
        can resume from stop_offset in a fresh context.
        """
        out = []
        offset = start_offset
        page_idx = 0
        while page_idx < MAX_PAGES_PER_DOC_TYPE:
            url = build_url(offset)
            try:
                page.goto("about:blank", wait_until="commit", timeout=5000)
            except Exception:
                pass
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as exc:
                log.warning("  page error [%s offset=%d]: %s", label, offset, exc)
                return out, offset, False
            self._await_ready(page, f"{label} offset={offset}", url)
            recs = parse(page.content())
            if not recs:
                return out, offset, False
            out.extend(recs)
            if len(recs) < PAGE_SIZE:
                return out, offset, True   # partial page == last page
            offset += PAGE_SIZE
            page_idx += 1
            time.sleep(0.6)
        return out, offset, True

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/124.0.0.0 Safari/537.36")

    _MAX_CONTEXT_RESTARTS = 12

    def _run_in_fresh_context(self, browser, build_url, parse, label) -> list:
        """One search = fresh browser context(s) (fresh cookies), each with
        its own homepage warmup. The portal only reliably serves the first
        search of a session; reusing a context zeroes later searches. If
        pagination dies mid-search (empty page after full pages), resume
        from that offset in another fresh context; a fresh context that
        still returns nothing at that offset is a genuine end."""
        out = []
        offset = 0
        for attempt in range(self._MAX_CONTEXT_RESTARTS):
            ctx = browser.new_context(
                user_agent=self._UA, viewport={"width": 1280, "height": 800})
            try:
                page = ctx.new_page()
                try:
                    page.goto(CLERK_BASE_URL, wait_until="domcontentloaded",
                              timeout=30000)
                except Exception as exc:
                    log.warning("  warmup failed [%s]: %s", label, exc)
                time.sleep(1.0)
                recs, offset, done = self._fetch_pages(
                    page, build_url, parse, label, start_offset=offset)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
            out.extend(recs)
            if done:
                return out
            if not recs:
                # Fresh context produced nothing at this offset: genuine
                # end (or empty search) rather than a session block.
                return out
            log.info("  [%s] resuming at offset %d in fresh context "
                     "(attempt %d)", label, offset, attempt + 2)
            time.sleep(1.5)
        return out

    def run(self) -> list:
        seen = set()
        all_records = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)

            def add_unique(recs):
                n = 0
                for r in recs:
                    key = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}"
                    if key and key not in seen:
                        seen.add(key)
                        all_records.append(r)
                        n += 1
                return n

            # ---- Real Property doc types (fresh context per search) ----
            rp_total = 0
            for doc_code, cat, cat_label in DOC_TYPES:
                log.info("Searching RP docType '%s' [%s]", doc_code, cat)
                recs = self._run_in_fresh_context(
                    browser, lambda off, dc=doc_code: self._build_url(dc, off),
                    lambda h, c=cat, cl=cat_label: self._parse_rp_html(h, c, cl),
                    doc_code)
                n = add_unique(recs)
                rp_total += n
                log.info("  -> %d unique records for '%s'", n, doc_code)
                time.sleep(1.5)

            # Zero-day guard: cert-date drift can silently empty the whole RP
            # pass. Re-run once with the window pulled back 4 more days.
            if rp_total == 0:
                log.warning("RP pass returned 0 records - retrying with "
                            "4-day certified-date clamp")
                for doc_code, cat, cat_label in DOC_TYPES:
                    recs = self._run_in_fresh_context(
                        browser,
                        lambda off, dc=doc_code: self._build_url(dc, off, clamp_extra_days=4),
                        lambda h, c=cat, cl=cat_label: self._parse_rp_html(h, c, cl),
                        f"{doc_code}(clamped)")
                    rp_total += add_unique(recs)
                    time.sleep(1.5)
                log.info("Clamped RP pass: %d records", rp_total)

            # ---- Foreclosures department (upcoming trustee sales) ----
            log.info("Searching FC department (upcoming trustee sales)")
            fc_recs = self._run_in_fresh_context(
                browser, self._build_fc_url, self._parse_fc_html, "FC")
            n = add_unique(fc_recs)
            log.info("  -> %d unique FC records", n)

            browser.close()
        log.info("Clerk portal: %d unique records collected", len(all_records))
        return all_records

# ---------------------------------------------------------------------------
# DCAD parcel enrichment (ArcGIS)
# ---------------------------------------------------------------------------
def _mailing_from_parcel(att: dict) -> tuple:
    street = _arc_val(att.get("PSTLADDRESS"))
    city = _arc_val(att.get("PSTLCITY")).title()
    state = _arc_val(att.get("PSTLSTATE")) or STATE
    zip_code = _arc_val(att.get("PSTLZIP5"))
    return street, city, state, zip_code


def _addr_key(addr: str) -> tuple:
    m = re.match(r"\s*(\d+)\s+(.*)", addr or "")
    if not m:
        return "", ""
    num = m.group(1)
    rest = _norm_ws(m.group(2))
    rest = re.sub(r"\s+(#|APT|UNIT|STE|SUITE|BLDG|LOT)\b.*$", "", rest, flags=re.I).strip()
    return num, rest


def _arcgis_query(session, where: str, count: int = 5) -> list:
    params = {
        "where": where,
        "outFields": "OWNERNME1,OWNERNME2,SITEADDRESS,PSTLADDRESS,PSTLCITY,"
                     "PSTLSTATE,PSTLZIP5,CNTASSDVAL,RESYRBLT",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": count,
    }
    try:
        r = session.get(PARCEL_API_URL, params=params, timeout=REQUEST_TIMEOUT)
        return r.json().get("features", []) or []
    except Exception as exc:
        log.debug("ArcGIS query error: %s", exc)
        return []


def enrich_parcels(records: list) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "DallasLeadScraper/1.0"

    # PASS 1 - forward by owner (fills situs on unique match, mailing always)
    fwd = [r for r in records if r.owner and (not r.prop_address or not r.mail_address)]
    log.info("DCAD owner-lookup for %d records...", len(fwd))
    owner_hits = 0
    for rec in fwd[:ARCGIS_MAX_LOOKUPS]:
        norm = normalize_owner_for_parcel(rec.owner)
        if not norm or len(norm) < 5:
            continue
        feats = _arcgis_query(
            session, f"UPPER(OWNERNME1) LIKE '{_sql_lit(norm)}%'")
        if not feats:
            continue
        att = feats[0].get("attributes", {})
        if not rec.prop_address and len(feats) == 1:
            situs = _arc_val(att.get("SITEADDRESS"))
            if situs:
                rec.prop_address = situs
        if not rec.mail_address:
            ms, mc, mst, mz = _mailing_from_parcel(att)
            if ms:
                rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
                owner_hits += 1
        time.sleep(0.12)
    log.info("DCAD owner-lookup: %d mailing fills", owner_hits)

    # PASS 2 - reverse by address (unique SITEADDRESS match only)
    rev = [r for r in records if r.prop_address and not r.owner]
    log.info("DCAD address-lookup for %d records...", len(rev))
    addr_hits = 0
    for rec in rev[:ARCGIS_MAX_LOOKUPS]:
        num, core = _addr_key(rec.prop_address)
        if not num or not core:
            continue
        feats = _arcgis_query(
            session, f"UPPER(SITEADDRESS) LIKE '{_sql_lit(num)} %{_sql_lit(core)}%'")
        if len(feats) == 1:
            att = feats[0].get("attributes", {})
            owner = _arc_val(att.get("OWNERNME1"))
            if owner:
                rec.owner = owner
                addr_hits += 1
            if not rec.mail_address:
                ms, mc, mst, mz = _mailing_from_parcel(att)
                if ms:
                    rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
        time.sleep(0.12)
    log.info("DCAD address-lookup: %d owner fills", addr_hits)

# ---------------------------------------------------------------------------
# Hash / dedupe identity + NEW-CHANGED detection
# ---------------------------------------------------------------------------
def _repo_base() -> Path:
    return Path(__file__).parent.parent


def _record_rid(r) -> str:
    basis = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}|{r.prop_address}"
    return hashlib.sha1(f"dallas|{basis}".encode()).hexdigest()[:16]


def _record_chash(r) -> str:
    fields = "|".join(str(x or "") for x in (
        r.doc_num, r.doc_type, r.filed, r.owner, r.grantee, r.legal,
        r.amount, r.prop_address, r.mail_address))
    return hashlib.sha1(fields.encode()).hexdigest()[:16]


def detect_changes(records: list) -> None:
    state_path = _repo_base() / "data" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    today = datetime.now().strftime("%Y-%m-%d")
    n_new = n_chg = n_exist = 0
    for r in records:
        r.rid = _record_rid(r)
        r.content_hash = _record_chash(r)
        prev = state.get(r.rid)
        if prev is None:
            r.status, r.first_seen = "NEW", today
            n_new += 1
        elif prev.get("content_hash") != r.content_hash:
            r.status = "CHANGED"
            r.first_seen = prev.get("first_seen", today)
            n_chg += 1
        else:
            r.status = "EXISTING"
            r.first_seen = prev.get("first_seen", today)
            n_exist += 1
        state[r.rid] = {"content_hash": r.content_hash,
                        "first_seen": r.first_seen, "last_seen": today}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log.info("NEW/CHANGED: NEW=%d CHANGED=%d EXISTING=%d (state=%d ids)",
             n_new, n_chg, n_exist, len(state))

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_records(records: list, start: datetime) -> None:
    for r in records:
        s, flags = 30, []
        if r.cat == "LP": s += 10; flags.append("LIS_PENDENS")
        if r.cat == "FC": s += 15; flags.append("FORECLOSURE")
        if r.cat == "TAXFC": s += 18; flags.append("TAX_FORECLOSURE")
        if r.cat == "TAXDEED": s += 10; flags.append("TAX_DEED")
        if r.cat in ("LP","FC","TAXFC"): s += 5
        if r.cat == "JUD": s += 8; flags.append("JUDGMENT")
        if r.cat == "LIEN": s += 7; flags.append("LIEN")
        if r.cat == "PRO": s += 12; flags.append("PROBATE")
        if r.amount > 100000: s += 15; flags.append("HIGH_AMOUNT")
        elif r.amount > 50000: s += 10; flags.append("MID_AMOUNT")
        if r.filed:
            try:
                if datetime.strptime(r.filed, "%Y-%m-%d") >= start:
                    s += 5; flags.append("NEW_THIS_WEEK")
            except ValueError:
                pass
        if r.prop_address:
            s += 5; flags.append("HAS_ADDRESS")
        r.score = min(s, 100)
        r.flags = flags

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DASH_CAT = {
    "LP": "foreclosure", "FC": "foreclosure", "TAXFC": "foreclosure",
    "TAXDEED": "tax_lien", "LIEN": "tax_lien",
    "JUD": "judgment", "PRO": "probate",
}
FLAG_NICE = {
    "LIS_PENDENS": "Lis pendens", "FORECLOSURE": "Pre-foreclosure",
    "TAX_FORECLOSURE": "Tax foreclosure", "TAX_DEED": "Tax deed",
    "JUDGMENT": "Judgment lien", "LIEN": "Tax lien",
    "PROBATE": "Probate / estate", "HIGH_AMOUNT": "Amount > $100k",
    "MID_AMOUNT": "Amount > $50k", "NEW_THIS_WEEK": "New this week",
    "HAS_ADDRESS": "Has address",
}


def write_outputs(records: list, start: datetime, end: datetime) -> None:
    base = _repo_base()
    for d in [base / "dashboard", base / "data"]:
        d.mkdir(parents=True, exist_ok=True)
    week_ago = (end - timedelta(days=7)).strftime("%Y-%m-%d")
    recs_out = []
    for r in records:
        d = asdict(r)
        d["cat_code"] = r.cat
        d["cat"] = DASH_CAT.get(r.cat, "tax_lien")
        d["flags"] = [FLAG_NICE.get(f, f) for f in (r.flags or [])]
        d["absentee"] = bool(
            r.prop_address and r.mail_address
            and r.prop_address.upper() != r.mail_address.upper())
        d["out_of_state"] = bool(r.mail_state and r.mail_state.upper() != STATE)
        recs_out.append(d)
    payload = {
        "fetched_at": datetime.utcnow().isoformat(),
        "county": COUNTY,
        "source": f"{COUNTY} County, {STATE} -- Clerk Portal (RP+FC) + DCAD Parcel API",
        "date_range": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
        "total": len(records),
        "new_7d": sum(1 for r in records if (r.first_seen or "") >= week_ago),
        "with_address": sum(1 for r in records if r.prop_address),
        "by_cat": {c: sum(1 for r in records if r.cat == c) for c in ("FC","TAXFC","TAXDEED","LP","JUD","LIEN","PRO")},
        "records": recs_out,
    }
    for path in [base / "dashboard" / "records.json", base / "data" / "records.json"]:
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info("JSON written: %s (%d records)", path, len(records))
    csv_path = base / "data" / "ghl_export.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(GHL_HEADERS.values()))
        writer.writeheader()
        for r in records:
            d = asdict(r)
            writer.writerow({GHL_HEADERS[k]: ("|".join(d[k]) if k=="flags" else d[k]) for k in GHL_FIELDS})
    log.info("GHL CSV written: %s (%d records)", csv_path, len(records))
    skip_path = base / "data" / "skiptrace_export.csv"
    skip_cols = ["First Name", "Last Name", "Mailing Address", "Mailing City",
                 "Mailing State", "Mailing Zip", "Property Address",
                 "Property City", "Property State", "Property Zip"]
    with open(skip_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=skip_cols)
        writer.writeheader()
        for r in records:
            owner = (r.owner or "").strip()
            if "," in owner:
                p = owner.split(",", 1)
                first, last = p[1].strip().title(), p[0].strip().title()
            else:
                # DCAD/clerk format is usually "LAST FIRST M"
                p = owner.split()
                first = p[1].title() if len(p) > 1 else ""
                last = p[0].title() if p else ""
            writer.writerow({
                "First Name": first, "Last Name": last,
                "Mailing Address": r.mail_address, "Mailing City": r.mail_city,
                "Mailing State": r.mail_state, "Mailing Zip": r.mail_zip,
                "Property Address": r.prop_address, "Property City": r.prop_city,
                "Property State": r.prop_state, "Property Zip": r.prop_zip,
            })
    log.info("Skip trace CSV written: %s (%d records)", skip_path, len(records))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dallas County lead scraper")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--probate-days", type=int, default=PROBATE_LOOKBACK_DAYS)
    parser.add_argument("--skip-parcel", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    # Clamp the recorded-date window to the portal's certification lag:
    # ranges past the certified-through date intermittently return empty.
    end = now - timedelta(days=CERT_LAG_DAYS)
    start = end - timedelta(days=args.days)
    probate_start = end - timedelta(days=args.probate_days)
    fc_end = now + timedelta(days=FC_HORIZON_DAYS)
    log.info("=" * 60)
    log.info("Dallas County Motivated Seller Lead Scraper")
    log.info("Lookback default=%dd  probate=%dd  cert-lag=%dd",
             args.days, args.probate_days, CERT_LAG_DAYS)
    log.info("=" * 60)
    log.info("Range: default %s->%s | probate %s->%s | FC sales %s->%s",
             start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"),
             probate_start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"),
             now.strftime("%m/%d/%Y"), fc_end.strftime("%m/%d/%Y"))
    scraper = ClerkScraper(start, end, probate_start, fc_end)
    records = scraper.run()
    if not args.skip_parcel:
        enrich_parcels(records)
    detect_changes(records)
    score_records(records, start)
    records.sort(key=lambda r: (r.status != "NEW", -r.score))
    if not records:
        log.warning("No records found. Writing empty output files.")
    else:
        log.info("Total after dedup + enrichment: %d", len(records))
    write_outputs(records, start, end)
    pro_count = sum(1 for r in records if r.cat == "PRO")
    pro_with_addr = sum(1 for r in records if r.cat == "PRO" and r.prop_address)
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  Total records  : %d", len(records))
    log.info("  With address   : %d", sum(1 for r in records if r.prop_address))
    log.info("  Probates       : %d (%d with address)", pro_count, pro_with_addr)
    log.info("  Score >= 70    : %d", sum(1 for r in records if r.score >= 70))
    log.info("  Score >= 50    : %d", sum(1 for r in records if r.score >= 50))


if __name__ == "__main__":
    main()
