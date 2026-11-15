#!/usr/bin/env python3
"""Fetch real CMS Medicare Part D prescribing for non-insulin diabetes drugs (live API) and
aggregate to one row per prescriber (NPI) -> total annual claims across those drugs.

Source: data.cms.gov  "Medicare Part D Prescribers - by Provider and Drug"
        dataset 9552739e-3d05-4c1b-8eff-ecabf391e2e5  (data year 2024 at time of writing)
Large files are cached OUTSIDE this folder (OneDrive): set NBA_CACHE (default ~/.cache/nba_pipeline).

Caveats built into the source (kept in the benchmark report):
  * Medicare Part D only (65+ / disabled), not all payers.
  * CMS suppresses prescriber-drug rows with fewer than 11 claims, so per-prescriber totals are
    lower-bounded and low-volume prescribers are missing (left truncation).

Run:  python3 -I 08_fetch_partd.py            (about 1.6M rows; several minutes)
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATASET = "9552739e-3d05-4c1b-8eff-ecabf391e2e5"
BASE = f"https://data.cms.gov/data-api/v1/dataset/{DATASET}/data"
CACHE = Path(os.environ.get("NBA_CACHE", Path.home() / ".cache" / "nba_pipeline"))
PAGE = 5000
COLUMNS = "Prscrbr_NPI,Prscrbr_State_Abrvtn,Prscrbr_Type,Gnrc_Name,Tot_Clms"

# Non-insulin diabetes drugs (generic names exactly as CMS spells them). Names with zero rows are skipped.
DRUGS = [
    "Metformin Hcl", "Glipizide", "Glimepiride", "Glyburide", "Glyburide Micronized", "Pioglitazone Hcl",
    "Sitagliptin Phosphate", "Sitagliptin/Metformin Hcl", "Linagliptin", "Saxagliptin Hcl", "Alogliptin Benzoate",
    "Empagliflozin", "Dapagliflozin Propanediol", "Canagliflozin", "Ertugliflozin",
    "Semaglutide", "Dulaglutide", "Liraglutide", "Tirzepatide",
    "Glipizide/Metformin Hcl", "Pioglitazone/Metformin Hcl", "Glyburide/Metformin Hcl",
]


def get(params, retries=4):
    url = BASE + "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-term-project/1.0 (academic)"})
            return json.load(urllib.request.urlopen(req, timeout=90))
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def row_count(drug):
    return get_stats({"filter[Gnrc_Name]": drug})


def get_stats(params):
    url = BASE + "/stats?" + urllib.parse.urlencode(params)
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nba-term-project/1.0"}), timeout=60))
    return d["found_rows"]


def fetch_page(args):
    drug, offset = args
    return get({"filter[Gnrc_Name]": drug, "size": PAGE, "offset": offset, "column": COLUMNS})


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    raw_path = CACHE / "partd_diabetes_rows.csv"
    tasks, plan = [], {}
    for d in DRUGS:
        n = row_count(d)
        plan[d] = n
        tasks += [(d, o) for o in range(0, n, PAGE)]
    print("Rows per drug:", {k: v for k, v in plan.items() if v})
    print(f"Total rows {sum(plan.values()):,} in {len(tasks)} requests", flush=True)

    agg = defaultdict(lambda: {"type": "", "state": "", "clms": 0.0, "drugs": set()})
    n_rows = 0
    with open(raw_path, "w", newline="") as fh, ThreadPoolExecutor(max_workers=4) as ex:
        w = csv.writer(fh)
        w.writerow(COLUMNS.split(","))
        for i, page in enumerate(ex.map(fetch_page, tasks), 1):
            for r in page:
                try:
                    clms = float(r["Tot_Clms"])
                except (TypeError, ValueError):
                    continue
                w.writerow([r["Prscrbr_NPI"], r["Prscrbr_State_Abrvtn"], r["Prscrbr_Type"], r["Gnrc_Name"], clms])
                a = agg[r["Prscrbr_NPI"]]
                a["type"], a["state"] = r["Prscrbr_Type"], r["Prscrbr_State_Abrvtn"]
                a["clms"] += clms
                a["drugs"].add(r["Gnrc_Name"])
                n_rows += 1
            if i % 25 == 0:
                print(f"  {i}/{len(tasks)} pages, {n_rows:,} rows", flush=True)

    out = CACHE / "partd_prescriber.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["npi", "specialty", "state", "total_claims", "n_drugs"])
        for npi, a in agg.items():
            w.writerow([npi, a["type"], a["state"], round(a["clms"], 1), len(a["drugs"])])
    meta = {"dataset": DATASET, "rows": n_rows, "prescribers": len(agg), "drugs": {k: v for k, v in plan.items() if v},
            "fetched_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    (CACHE / "partd_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Done: {n_rows:,} rows -> {len(agg):,} prescribers. Wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
