#!/usr/bin/env python3
"""NPPES roster check: does the synthetic endocrinologist roster look geographically like the real one?

Compares the state distribution of GLUCOREN's endocrinologists with the real count of individual
endocrinologists per state from the NPPES NPI Registry (nba.ref_nppes_endo_state, loaded by 09_ref_data.py).

Scope and caveats (also in the output JSON):
  * Compares SHARES of the national total, so differences in roster size do not matter.
  * GLUCOREN is a sample of ~633 endocrinologists, so small states have noisy shares.
  * The NPPES API returns at most 1,200 records per query; states that hit that cap are undercounted.
  * Only endocrinologists are checked: NPPES cannot count PCPs or NPs/PAs through this API (the result
    sets are far larger than the 1,200-record cap).
Run:  python3 -I 11_roster_check.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def main():
    sim = common.bq_df("SELECT state, region, COUNT(*) AS sim_n FROM nba.raw_hcp_master "
                       "WHERE specialty = 'Endocrinology' GROUP BY 1, 2")
    real = common.bq_df("SELECT state, n_endocrinologists AS real_n, count_capped FROM nba.ref_nppes_endo_state")
    df = real.merge(sim, on="state", how="left").fillna({"sim_n": 0})
    df["real_share"] = df.real_n / df.real_n.sum()
    df["sim_share"] = df.sim_n / df.sim_n.sum()
    capped = df[df.count_capped]
    tvd_state = 0.5 * float((df.real_share - df.sim_share).abs().sum())
    r_p, r_s = pearsonr(df.real_share, df.sim_share)[0], spearmanr(df.real_share, df.sim_share)[0]

    # regional comparison using GLUCOREN's own state -> region map
    sm = common.bq_df("SELECT state, ANY_VALUE(region) AS region FROM nba.raw_hcp_master GROUP BY 1")
    d2 = df.drop(columns=["region"]).merge(sm, on="state", how="left")
    reg = d2.groupby("region")[["real_share", "sim_share"]].sum().round(3)
    tvd_reg = 0.5 * float((reg.real_share - reg.sim_share).abs().sum())

    top_real = df.nlargest(6, "real_n")[["state", "real_n", "real_share", "sim_share"]].round(3)
    out = {"states": int(len(df)), "real_total": int(df.real_n.sum()), "sim_total": int(df.sim_n.sum()),
           "states_capped_by_api": capped.state.tolist(),
           "state_share_pearson": round(float(r_p), 3), "state_share_spearman": round(float(r_s), 3),
           "state_total_variation_distance": round(tvd_state, 3), "region_total_variation_distance": round(tvd_reg, 3),
           "region_shares": reg.reset_index().to_dict("records"),
           "largest_real_states": top_real.to_dict("records"),
           "caveats": ["shares compared (roster sizes differ)", "NPPES API caps at 1,200 records per query",
                       "endocrinology only; PCP and NP/PA counts are not retrievable through this API"]}
    (HERE / "outputs").mkdir(exist_ok=True)
    (HERE / "outputs" / "roster_check.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k not in ("region_shares", "largest_real_states")}, indent=2))
    print("\nRegional share of endocrinologists:\n", reg.to_string())
    print("\nLargest real states:\n", top_real.to_string(index=False))


if __name__ == "__main__":
    main()
