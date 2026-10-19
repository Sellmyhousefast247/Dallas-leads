#!/usr/bin/env python3
"""Build the auto-updating SURPLUS FUNDS dashboard from this run's own data.

Inputs  : data/trustee_deeds.csv  (recorded trustee deeds + OCR'd figures)
          data/records.json       (same-run liens/judgments for owner cross-reference)
Outputs : data/surplus_leads.json (computed leads, machine-readable)
          dashboard/surplus.html  (published by the existing Pages deploy)

Honest-math rules, enforced here exactly like in the overage engine:
- Winning bid comes only from the deed (detail text or OCR). Blank = unknown, never $0.
- The ONLY auto-available debt figure in TX non-judicial is the ORIGINAL principal from the
  deed's own recital. Payoff != original principal, so every spread computed from it is
  labeled PROVISIONAL and the dashboard never calls it verified or recoverable.
- Liens are cross-referenced by owner name against the same run's recorded liens/judgments.
  The clerk grid carries no balances, so matches are flagged exposure, never dollar-netted.
- Lender/financial-institution buyers are buybacks: no surplus play, status LENDER_BUYBACK.
"""
from __future__ import annotations

import csv
import html
import json
import pathlib
import re
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT_JSON = DATA / "surplus_leads.json"
OUT_HTML = ROOT / "dashboard" / "surplus.html"
MIN_SPREAD = 10_000

FINANCIAL_RE = re.compile(
    r"\b(BANK|MORTGAGE|LENDING|HOME LOANS?|SAVINGS|CREDIT UNION|FCU|N\.?A\.?|"
    r"LOAN (SERVICING|TRUST)|SERVICING|FINANCIAL|REVERSE|FANNIE|FREDDIE|HUD|"
    r"SECRETARY OF HOUSING|VETERANS AFFAIRS|MERS)\b", re.IGNORECASE)
NOISE_RE = re.compile(r"\b(LLC|INC|CORP(ORATION)?|CO|LP|LTD|TR(USTEE)?|ET ?AL|AND|&|JR|SR|II|III|IV)\b\.?")


def _tokens(name: str) -> set:
    n = NOISE_RE.sub(" ", (name or "").upper())
    return {t for t in re.sub(r"[^A-Z ]", " ", n).split() if len(t) > 2}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _num(s):
    try:
        return float(str(s).replace(",", "")) if s not in (None, "") else None
    except ValueError:
        return None


def load_liens() -> list:
    try:
        recs = json.loads((DATA / "records.json").read_text()).get("records", [])
    except Exception:
        return []
    return [r for r in recs if r.get("cat_code") in ("LIEN", "JUD", "LP")]


def build_leads() -> list:
    liens = load_liens()
    leads = []
    with (DATA / "trustee_deeds.csv").open() as f:
        for row in csv.DictReader(f):
            buyer = (row.get("grantee") or "").strip()
            owner = (row.get("owner_full") or row.get("mortgagors") or "").strip()
            price = _num(row.get("consideration"))
            principal = _num(row.get("original_principal"))
            assessed = _num(row.get("assessed_value"))

            spread = price - principal if (price is not None and principal is not None) else None

            lien_hits = []
            if owner:
                for ln in liens:
                    if _overlap(owner, ln.get("owner") or "") >= 0.67:
                        lien_hits.append({"type": ln.get("cat_label"), "doc": ln.get("doc_num"),
                                          "filed": ln.get("filed"), "url": ln.get("clerk_url")})

            beneficiary = (row.get("beneficiary") or "").strip()
            if buyer and beneficiary and _overlap(buyer, beneficiary) >= 0.5:
                status, why = "LENDER_BUYBACK", "buyer matches the deed's own lender/beneficiary — credit-bid buyback"
            elif buyer and FINANCIAL_RE.search(buyer):
                status, why = "LENDER_BUYBACK", "financial-institution grantee — no surplus play"
            elif price is None:
                status, why = "NEEDS_PRICE", "winning bid not extracted — open deed PDF"
            elif principal is None:
                status, why = "NEEDS_DEBT", "original principal not extracted — open deed PDF"
            elif spread is not None and spread >= MIN_SPREAD:
                status = "SCREEN_HIT"
                why = (f"provisional spread ${spread:,.0f} (bid − ORIGINAL principal; actual "
                       "payoff unverified)") + (f"; {len(lien_hits)} lien/judgment record(s) on "
                                                "owner name — exposure unknown" if lien_hits else "")
            elif spread is not None and spread > 0:
                status, why = "BELOW_MIN", f"provisional spread ${spread:,.0f} under ${MIN_SPREAD:,}"
            else:
                status, why = "NO_SPREAD", "bid at or under original principal — payoff spread unlikely"

            leads.append({
                "county": (row.get("county") or "dallas").title(),
                "instrument": row.get("instrument_number"),
                "recorded": row.get("recording_date"),
                "sale_date": row.get("sale_date") or "",
                "property": row.get("property_address") or row.get("legal") or "",
                "owner": owner,
                "owner_source": "appraisal district" if row.get("owner_full") else (
                    "deed OCR" if row.get("mortgagors") else ""),
                "mail_address": row.get("mail_address") or "",
                "assessed_value": assessed,
                "buyer": buyer,
                "sale_price": price,
                "original_principal": principal,
                "provisional_spread": spread,
                "lien_hits": lien_hits,
                "status": status,
                "why": why,
                "deed_url": row.get("clerk_url"),
            })
    order = {"SCREEN_HIT": 0, "NEEDS_DEBT": 1, "NEEDS_PRICE": 2, "BELOW_MIN": 3,
             "NO_SPREAD": 4, "LENDER_BUYBACK": 5}
    leads.sort(key=lambda l: (order.get(l["status"], 9), -(l["provisional_spread"] or 0)))
    return leads


def render(leads: list) -> str:
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n = lambda s: sum(1 for l in leads if l["status"] == s)
    spread_total = sum(l["provisional_spread"] or 0 for l in leads if l["status"] == "SCREEN_HIT")
    cards = [
        ("Deeds analyzed", len(leads), ""), ("Screen hits ≥ $10K", n("SCREEN_HIT"), "hit"),
        ("Provisional spread (hits)", f"${spread_total:,.0f}", "money"),
        ("Needs deed data", n("NEEDS_PRICE") + n("NEEDS_DEBT"), "warn"),
        ("Lender buybacks", n("LENDER_BUYBACK"), "mut"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v {c}">{v}</div></div>'
        for k, v, c in cards)
    rows_html = []
    for l in leads:
        money = lambda v: f"${v:,.0f}" if v is not None else "—"
        liens_txt = ", ".join(h["type"] or "lien" for h in l["lien_hits"][:4]) or "—"
        e = lambda s: html.escape(str(s or ""))
        rows_html.append(f"""<tr>
<td>{e(l['county'])}<br><span class=mut>{e(l['instrument'])}</span></td>
<td class=wrap>{e(l['property'])[:90]}</td><td>{e(l['sale_date'] or l['recorded'])}</td>
<td class=wrap>{e(l['owner']) or '—'}{('<br><span class=mut>' + e(l['owner_source']) + '</span>') if l['owner_source'] else ''}</td>
<td class=wrap>{e(l['mail_address']) or '—'}</td>
<td class=wrap>{e(l['buyer'])}</td>
<td class=m>{money(l['sale_price'])}</td><td class=m>{money(l['original_principal'])}</td>
<td class=m>{money(l['assessed_value'])}</td>
<td class="m hit">{money(l['provisional_spread'])}</td>
<td class=wrap>{e(liens_txt)}</td>
<td><span class="badge s-{l['status']}">{l['status'].replace('_',' ')}</span><br>
<span class=mut>{e(l['why'])[:140]}</span></td>
<td><a href="{e(l['deed_url'])}" target="_blank" rel="noopener">deed</a></td></tr>""")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Surplus Funds — TX Trustee Deeds</title>
<style>
:root{{--bg:#0b0f14;--panel:#121923;--line:#1f2a38;--text:#dbe4ee;--mut:#7e8da0;--green:#34d399;--amber:#fbbf24;--red:#f87171;--blue:#60a5fa}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:400 14px/1.45 "Segoe UI",system-ui,sans-serif;padding:16px 20px}}
h1{{font-size:1.1rem;margin:0}}h1 span{{color:var(--green)}}.sub{{color:var(--mut);font-size:.78rem;margin:2px 0 6px}}
.note{{display:inline-block;border:1px solid var(--line);color:var(--amber);border-radius:99px;padding:3px 12px;font-size:.72rem;margin-bottom:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:12px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
.card .k{{font-size:.68rem;text-transform:uppercase;letter-spacing:.8px;color:var(--mut)}}
.card .v{{font-size:1.35rem;font-weight:600;font-variant-numeric:tabular-nums}}
.v.hit,.v.money{{color:var(--green)}}.v.warn{{color:var(--amber)}}.v.mut{{color:var(--mut)}}
.tablewrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}}
table{{border-collapse:collapse;width:100%;min-width:1150px;font-size:.78rem}}
th,td{{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}}
td.wrap{{white-space:normal;max-width:220px}}td.m{{text-align:right;font-variant-numeric:tabular-nums}}
th{{position:sticky;top:0;background:#0e141d;color:var(--mut);font-size:.65rem;text-transform:uppercase;letter-spacing:.6px}}
.m.hit{{color:var(--green);font-weight:600}}.mut{{color:var(--mut)}}
.badge{{display:inline-block;padding:1px 8px;border-radius:99px;font-size:.65rem;font-weight:600;border:1px solid var(--line)}}
.s-SCREEN_HIT{{color:var(--green);border-color:var(--green)}}.s-NEEDS_PRICE,.s-NEEDS_DEBT,.s-BELOW_MIN{{color:var(--amber);border-color:var(--amber)}}
.s-LENDER_BUYBACK,.s-NO_SPREAD{{color:var(--red);border-color:var(--red)}}
a{{color:var(--blue);text-decoration:none}}.foot{{color:var(--mut);font-size:.7rem;margin-top:10px}}
</style></head><body>
<h1>SURPLUS FUNDS <span>— TX TRUSTEE DEED SCREEN</span></h1>
<div class=sub>Auto-generated from recorded substitute trustee's deeds · updated {gen} · refreshes with each scrape run</div>
<div class=note>Spreads are PROVISIONAL: winning bid − ORIGINAL loan principal from the deed's recital. Actual payoff, costs and lien balances are unverified until pulled per case.</div>
<div class=cards>{card_html}</div>
<div class=tablewrap><table><thead><tr>
<th>County / Instr.</th><th>Property</th><th>Sale date</th><th>Former owner</th><th>Mailing addr</th><th>Buyer</th>
<th>Sale price</th><th>Orig. principal</th><th>Assessed</th><th>Prov. spread</th><th>Liens on owner</th><th>Status</th><th>Doc</th>
</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>
<div class=foot>{len(leads)} deeds · lender buybacks are excluded from opportunity counts · lien matches are name-based flags from this run's recorded liens/judgments (no balances published)</div>
</body></html>"""


def main() -> None:
    if not (DATA / "trustee_deeds.csv").exists():
        print("no trustee_deeds.csv — skipping surplus dashboard")
        return
    leads = build_leads()
    OUT_JSON.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                                    "leads": leads}, indent=1))
    OUT_HTML.write_text(render(leads))
    hits = sum(1 for l in leads if l["status"] == "SCREEN_HIT")
    print(f"surplus.html: {len(leads)} deeds, {hits} screen hits")


if __name__ == "__main__":
    main()
