#!/usr/bin/env python3
"""Weekly DCAD bulk sync: download the county's full current-ownership export and build a
compact local lookup used to cross-reference every trustee deed (owner, situs address,
mailing address, legal, value) without per-record API calls.

Source: DCAD Data Products — DCAD2027_CURRENT.zip ("Most Current Ownership"), free public
data published by the Dallas Central Appraisal District at dallascad.org/DataProducts.aspx.

Output: data/dcad_lookup.csv.gz (NOT committed; restored from Actions cache daily and
rebuilt when older than REFRESH_DAYS) + data/dcad_lookup.meta.json.

Column mapping is header-driven (regex over the CSV header), so DCAD renaming or
reordering columns degrades gracefully instead of silently mis-mapping.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import pathlib
import re
import sys
import zipfile
from datetime import datetime, timezone

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOOKUP = DATA / "dcad_lookup.csv.gz"
META = DATA / "dcad_lookup.meta.json"
REFRESH_DAYS = 6

DCAD_URL = ("https://www.dallascad.org/ViewPDFs.aspx?type=3&id="
            "%5C%5CDCAD.ORG%5CWEB%5CWEBDATA%5CWEBFORMS%5Cdata%20products%5CDCAD2027_CURRENT.zip")

OUT_FIELDS = ["account", "owner", "mail_address", "situs_address", "situs_city",
              "situs_zip", "legal", "value"]

# header-pattern → logical field(s)
H = {
    "account": re.compile(r"^ACCOUNT[_ ]?NUM", re.I),
    "owner1": re.compile(r"^OWNER[_ ]?NAME1?$|^OWNER1", re.I),
    "owner2": re.compile(r"^OWNER[_ ]?NAME2$|^OWNER2", re.I),
    "biz": re.compile(r"^BIZ[_ ]?NAME", re.I),
    "m1": re.compile(r"^OWNER[_ ]?ADDRESS[_ ]?LINE?1|^MAIL.*1", re.I),
    "m2": re.compile(r"^OWNER[_ ]?ADDRESS[_ ]?LINE?2", re.I),
    "m3": re.compile(r"^OWNER[_ ]?ADDRESS[_ ]?LINE?3", re.I),
    "m4": re.compile(r"^OWNER[_ ]?ADDRESS[_ ]?LINE?4", re.I),
    "mcity": re.compile(r"^OWNER[_ ]?CITY|^MAIL.*CITY", re.I),
    "mstate": re.compile(r"^OWNER[_ ]?STATE", re.I),
    "mzip": re.compile(r"^OWNER[_ ]?ZIP", re.I),
    "snum": re.compile(r"^STREET[_ ]?NUM", re.I),
    "shalf": re.compile(r"^STREET[_ ]?HALF", re.I),
    "sname": re.compile(r"^FULL[_ ]?STREET|^STREET[_ ]?NAME", re.I),
    "scity": re.compile(r"^PROPERTY[_ ]?CITY|^SITUS[_ ]?CITY", re.I),
    "szip": re.compile(r"^PROPERTY[_ ]?ZIP|^SITUS[_ ]?ZIP", re.I),
    "l1": re.compile(r"^LEGAL1$", re.I), "l2": re.compile(r"^LEGAL2$", re.I),
    "l3": re.compile(r"^LEGAL3$", re.I), "l4": re.compile(r"^LEGAL4$", re.I),
    "value": re.compile(r"^TOT(AL)?[_ ]?VAL", re.I),
}


def _map_headers(header: list) -> dict:
    cols = {}
    for i, name in enumerate(header):
        for key, pat in H.items():
            if key not in cols and pat.match(name.strip()):
                cols[key] = i
    return cols


def _is_fresh() -> bool:
    if not (LOOKUP.exists() and META.exists()):
        return False
    try:
        built = datetime.fromisoformat(json.loads(META.read_text())["built_at"])
        age = (datetime.now(timezone.utc) - built).days
        return age < REFRESH_DAYS
    except Exception:
        return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def build() -> None:
    DATA.mkdir(exist_ok=True)
    if _is_fresh():
        print("dcad lookup fresh — skipping download")
        return
    print("downloading DCAD current-ownership export (large file)…")
    tmp = DATA / "dcad_current.zip"
    with requests.get(DCAD_URL, stream=True, timeout=120,
                      headers={"User-Agent": "DallasLeadScraper/1.0"}) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    print(f"downloaded {tmp.stat().st_size/1e6:.0f} MB")

    rows_out = 0
    values: dict = {}
    with zipfile.ZipFile(tmp) as z:
        names = z.namelist()
        acct_name = next((n for n in names if re.search(r"account_info.*\.(csv|txt)$", n, re.I)), None)
        if not acct_name:
            raise SystemExit(f"account_info csv not found in zip; contents: {names[:20]}")
        # optional values file
        val_name = next((n for n in names if re.search(r"(apprl|appraisal).*(year)?.*\.(csv|txt)$", n, re.I)), None)
        if val_name:
            with z.open(val_name) as f:
                rdr = csv.reader(io.TextIOWrapper(f, "latin-1", errors="replace"))
                vh = _map_headers(next(rdr))
                if "account" in vh and "value" in vh:
                    for row in rdr:
                        try:
                            values[row[vh["account"]].strip()] = row[vh["value"]].strip()
                        except IndexError:
                            continue
            print(f"values loaded: {len(values):,} from {val_name}")

        with z.open(acct_name) as f, gzip.open(LOOKUP, "wt", newline="") as out:
            rdr = csv.reader(io.TextIOWrapper(f, "latin-1", errors="replace"))
            cols = _map_headers(next(rdr))
            missing = [k for k in ("account", "owner1") if k not in cols]
            if missing:
                raise SystemExit(f"required columns not found {missing}; header mapping: {cols}")
            w = csv.writer(out)
            w.writerow(OUT_FIELDS)
            def g(row, key):
                i = cols.get(key, -1)
                return _norm(row[i]) if 0 <= i < len(row) else ""
            for row in rdr:
                acct = g(row, "account")
                owner = _norm(" ".join(x for x in (g(row, "owner1"), g(row, "owner2")) if x)) or g(row, "biz")
                if not acct or not owner:
                    continue
                situs = _norm(" ".join(x for x in (g(row, "snum"), g(row, "shalf"), g(row, "sname")) if x))
                mail = _norm(", ".join(x for x in (
                    " ".join(y for y in (g(row, "m1"), g(row, "m2"), g(row, "m3"), g(row, "m4")) if y),
                    g(row, "mcity"), f"{g(row,'mstate')} {g(row,'mzip')}".strip()) if x))
                legal = _norm(" ".join(g(row, k) for k in ("l1", "l2", "l3", "l4")))
                w.writerow([acct, owner, mail, situs, g(row, "scity"), g(row, "szip"),
                            legal, values.get(acct, "")])
                rows_out += 1
    tmp.unlink(missing_ok=True)
    META.write_text(json.dumps({"built_at": datetime.now(timezone.utc).isoformat(),
                                "rows": rows_out, "source": DCAD_URL}))
    print(f"dcad_lookup.csv.gz built: {rows_out:,} parcels")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        # never break the daily flow — the deed enrichment falls back to the ArcGIS API
        print(f"WARNING dcad bulk sync failed: {exc}", file=sys.stderr)
        sys.exit(0)
