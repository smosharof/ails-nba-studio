#!/usr/bin/env python3
"""Load real NPPES reference data into BigQuery (aggregate counts only; no individual-level data).

  nba.ref_nppes_endo_state : NPPES NPI Registry count of individual endocrinologists per state.

The counts are AGGREGATE (by state), so they cannot identify any synthetic HCP. They are used only as a
roster check (11_roster_check.py), not as a model feature.

Caveat: the NPPES API returns at most 1,200 records per query, so states that reach that cap are undercounted
(California does). Only endocrinology is counted; PCP and NP/PA result sets are far larger than the cap.

Run:  python3 -I 09_ref_data.py
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
          "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()


def http_json(url, retries=4):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-term-project/1.0 (academic)"})
            return json.load(urllib.request.urlopen(req, timeout=150))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(3 * (i + 1))


def nppes():
    rows = []
    for st in STATES:
        npis, skip, capped = set(), 0, False
        while True:
            u = ("https://npiregistry.cms.hhs.gov/api/?version=2.1&enumeration_type=NPI-1&limit=200"
                 f"&taxonomy_description={urllib.parse.quote('Endocrinology')}&state={st}&skip={skip}")
            res = http_json(u).get("results", [])
            for r in res:
                for t in r.get("taxonomies", []):
                    if t.get("primary") and t.get("desc") == "Internal Medicine, Endocrinology, Diabetes & Metabolism":
                        npis.add(r["number"])
            if len(res) < 200:
                break
            skip += 200
            if skip > 1000:       # API hard cap: 1,200 records per query
                capped = True
                break
            time.sleep(0.2)
        rows.append({"state": st, "n_endocrinologists": len(npis), "count_capped": capped})
        time.sleep(0.2)
    df = pd.DataFrame(rows)
    print(f"NPPES: {int(df.n_endocrinologists.sum()):,} endocrinologists across {len(df)} states; "
          f"{int(df.count_capped.sum())} states hit the API cap")
    return df


def main():
    common.bq_load_df(nppes(), "ref_nppes_endo_state")


if __name__ == "__main__":
    main()
