# Dallas-leads

Motivated-seller lead scraper for **Dallas County, TX**. Cloned from the
Bexar/Milwaukee county scraper systems.

**Live dashboard:** https://sellmyhousefast247.github.io/Dallas-leads/

## Sources
- **Clerk portal** (GovOS): https://dallas.tx.publicsearch.us
  - `department=RP` — lis pendens, appointment of substitute trustee,
    abstracts of judgment, judgments, state/federal tax liens, hospital
    liens, child-support liens, mechanic's liens, HOA assessment liens,
    tax deeds/sales, affidavits of heirship, probate proceedings
  - `department=FC` — upcoming trustee-sale (foreclosure) notices with
    sale dates. Street addresses live only inside the scanned notice
    PDFs, so FC records carry city + sale date + deep link.
- **DCAD parcels** (ArcGIS):
  `maps.dcad.org/prdwa/rest/services/Property/ParcelQuery/MapServer/4`
  — owner-forward and address-reverse enrichment (situs + mailing).

## Pipeline
County → scrape → normalize → hash/dedupe → NEW/CHANGED detection →
score → export (`dashboard/records.json`, `data/ghl_export.csv`,
`data/skiptrace_export.csv`). State in `data/state.json`.

## Runs
Daily via GitHub Actions (13:00 UTC) + manual `workflow_dispatch`.

## Dallas-specific notes
- RP result grid has **no street-address column** (Town + Legal only);
  addresses come from DCAD enrichment.
- Recorded-date windows past the portal's "Certified through" date can
  silently return empty; the scraper clamps end-date by 3 days and
  re-runs once with a wider clamp if the whole pass comes back empty.
- Doc-type filtering uses `searchType=quickSearch` + `_docTypes=<CODE>`
  (the `advancedSearch&docTypes=` pattern from Bexar is not honored).
- Possible future upgrade: pull street addresses for FC notices from the
  free monthly PDFs at dallascounty.org (Find Foreclosure Notices).
