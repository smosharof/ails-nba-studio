#!/usr/bin/env python3
"""Part D benchmark check: is the simulated GLUCOREN prescribing distribution realistic?

Compares GLUCOREN's annual per-HCP prescribing to REAL Medicare Part D diabetes-drug volumes by specialty.
Inputs : cache from 08_fetch_partd.py (NBA_CACHE) + GLUCOREN tables in BigQuery.
Outputs: outputs/partd_benchmark.csv / .json / .png, and the BigQuery table nba.ref_partd_benchmark
         (specialty x state quantiles, used as a market-size benchmark feature in 02_build_features.sql).

How the two sides are made comparable (and where they are not):
  * GLUCOREN 'market_trx' covers all payers; Part D is Medicare only -> GLUCOREN volume is scaled by each
    HCP's pct_medicare ("Medicare-equivalent" volume).
  * CMS drops prescriber-drug rows under 11 claims (left truncation) -> GLUCOREN is truncated the same way.
  * Levels differ by data year (2025 vs 2024), drug list and what counts as a 'market' drug, so the primary
    test is SHAPE (spread and skew, with each side divided by its own median), plus specialty ordering.
Run:  NBA_CACHE=<dir> python3 -I 10_partd_benchmark.py
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

CACHE = Path(os.environ.get("NBA_CACHE", Path.home() / ".cache" / "nba_pipeline"))
OUT = HERE / "outputs"
OUT.mkdir(exist_ok=True)
GROUPS = ["PCP", "NP/PA", "Endocrinology", "Cardiology"]
MAP = {"Family Practice": "PCP", "Internal Medicine": "PCP", "General Practice": "PCP",
       "Nurse Practitioner": "NP/PA", "Physician Assistant": "NP/PA",
       "Endocrinology": "Endocrinology", "Cardiology": "Cardiology"}
MIN_CLAIMS = 11


def q(x, p):
    return float(np.percentile(x, p))


def main():
    pdx = pd.read_csv(CACHE / "partd_prescriber.csv", dtype={"npi": str})
    meta = json.loads((CACHE / "partd_meta.json").read_text())
    pdx["group"] = pdx.specialty.map(MAP).fillna("Other")
    print(f"Part D: {len(pdx):,} prescribers of non-insulin diabetes drugs (dataset rows {meta['rows']:,})")

    g = common.bq_df("""
        SELECT m.hcp_id, m.specialty AS grp, m.state, m.region, m.pct_medicare,
               SUM(p.market_trx) AS mkt_2025
        FROM nba.raw_hcp_master m JOIN nba.raw_hcp_monthly p USING (hcp_id)
        WHERE p.month >= '2025-01' AND p.month <= '2025-12'
        GROUP BY 1,2,3,4,5""")
    g["medicare_equiv"] = g.mkt_2025 * g.pct_medicare
    trunc = g[g.medicare_equiv >= MIN_CLAIMS]

    rows, ks_rows = [], []
    for grp in GROUPS:
        real = pdx[pdx.group == grp].total_claims.values
        sim_all = g[g.grp == grp].medicare_equiv.values
        sim = trunc[trunc.grp == grp].medicare_equiv.values
        r_med, s_med = np.median(real), np.median(sim)
        ks = ks_2samp(np.log(real / r_med), np.log(sim / s_med))
        for name, x in (("Part D (real, 2024)", real), ("GLUCOREN (sim, Medicare-equivalent, truncated)", sim)):
            rows.append({"specialty": grp, "source": name, "n": len(x), "mean": round(x.mean(), 1),
                         "p10": round(q(x, 10), 1), "p25": round(q(x, 25), 1), "p50": round(q(x, 50), 1),
                         "p75": round(q(x, 75), 1), "p90": round(q(x, 90), 1),
                         "p90_over_p50": round(q(x, 90) / q(x, 50), 2), "p10_over_p50": round(q(x, 10) / q(x, 50), 2),
                         "log_sd": round(float(np.std(np.log(x))), 3)})
        ks_rows.append({"specialty": grp, "median_ratio_sim_over_real": round(float(s_med / r_med), 2),
                        "shape_KS_stat": round(float(ks.statistic), 3), "shape_KS_p": float(f"{ks.pvalue:.3g}"),
                        "sim_n_before_truncation": len(sim_all), "sim_n_after": len(sim)})
    tab = pd.DataFrame(rows)
    ksd = pd.DataFrame(ks_rows)

    # specialty ordering: median volume relative to PCP, real vs simulated
    pcp_r = np.median(pdx[pdx.group == "PCP"].total_claims)
    pcp_s = np.median(trunc[trunc.grp == "PCP"].medicare_equiv)
    order = pd.DataFrame([{"specialty": s,
                           "real_median_vs_PCP": round(float(np.median(pdx[pdx.group == s].total_claims) / pcp_r), 2),
                           "sim_median_vs_PCP": round(float(np.median(trunc[trunc.grp == s].medicare_equiv) / pcp_s), 2)}
                          for s in GROUPS])

    # specialty mix and regional mix (shares), real prescribers vs simulated roster
    state_region = g.dropna(subset=["state", "region"]).groupby("state").region.agg(lambda s: s.mode().iloc[0])
    real_mix = pdx[pdx.group.isin(GROUPS)].group.value_counts(normalize=True)
    sim_mix = g[g.grp.isin(GROUPS)].grp.value_counts(normalize=True)
    mix = pd.DataFrame({"real_share": real_mix, "sim_share": sim_mix}).loc[GROUPS].round(3)
    pdx["region"] = pdx.state.map(state_region)
    reg_real = pdx.dropna(subset=["region"]).region.value_counts(normalize=True)
    reg_sim = g.region.value_counts(normalize=True)
    reg = pd.DataFrame({"real_share": reg_real, "sim_share": reg_sim}).fillna(0).round(3)
    tv = float(0.5 * (reg.real_share - reg.sim_share).abs().sum())

    print("\n=== Distribution by specialty ===\n", tab.to_string(index=False))
    print("\n=== Shape/level comparison ===\n", ksd.to_string(index=False))
    print("\n=== Median volume relative to PCP ===\n", order.to_string(index=False))
    print("\n=== Specialty mix (share of prescribers) ===\n", mix.to_string())
    print("\n=== Regional mix ===\n", reg.to_string(), f"\nTotal variation distance (regions): {tv:.3f}")

    tab.to_csv(OUT / "partd_benchmark.csv", index=False)
    (OUT / "partd_benchmark.json").write_text(json.dumps({
        "partd_meta": meta, "shape_and_level": ks_rows, "ordering": order.to_dict("records"),
        "specialty_mix": mix.reset_index().rename(columns={"index": "specialty"}).to_dict("records"),
        "region_total_variation_distance": round(tv, 3), "region_mix": reg.reset_index().to_dict("records"),
        "caveats": ["Part D = Medicare only; GLUCOREN scaled by pct_medicare",
                    "Part D suppresses prescriber-drug rows < 11 claims; GLUCOREN truncated identically",
                    "Different years (2024 vs 2025) and drug lists; shape is the primary comparison"]}, indent=2, default=str))

    # ---- plot -----------------------------------------------------------------------------------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axs = plt.subplots(2, 2, figsize=(9, 6.2), sharey=False)
        for ax, grp in zip(axs.ravel(), GROUPS):
            real = np.log10(pdx[pdx.group == grp].total_claims.values)
            sim = np.log10(trunc[trunc.grp == grp].medicare_equiv.values)
            bins = np.linspace(min(real.min(), sim.min()), max(np.percentile(real, 99.5), np.percentile(sim, 99.5)), 40)
            ax.hist(real, bins=bins, density=True, alpha=0.55, color="#1f6f8b", label="Part D (real, 2024)")
            ax.hist(sim, bins=bins, density=True, alpha=0.55, color="#d9822b", label="GLUCOREN (sim)")
            ax.set_title(grp)
            ax.set_xlabel("log10 annual claims per prescriber (Medicare-equivalent)")
        axs[0, 0].legend(frameon=False, fontsize=8)
        fig.suptitle("Prescribing volume per HCP: real Medicare Part D vs simulated GLUCOREN", fontsize=11)
        fig.tight_layout()
        fig.savefig(OUT / "partd_benchmark.png", dpi=150)
        print("Saved outputs/partd_benchmark.png")
    except Exception as e:
        print("Plot skipped:", e)

    # ---- benchmark table for BigQuery (specialty x state, plus national 'US') ------------------------------
    rec = []
    for grp in GROUPS:
        sub = pdx[pdx.group == grp]
        for st, s in list(sub.groupby("state")) + [("US", sub)]:
            x = s.total_claims.values
            rec.append({"specialty_group": grp, "state": st, "n_prescribers": len(x), "mean_clms": float(x.mean()),
                        "p10": q(x, 10), "p25": q(x, 25), "p50": q(x, 50), "p75": q(x, 75), "p90": q(x, 90),
                        "data_year": 2024})
    ref = pd.DataFrame(rec).round(2)
    common.bq_load_df(ref, "ref_partd_benchmark")
    print(f"Loaded nba.ref_partd_benchmark: {len(ref)} rows (specialty x state + national)")


if __name__ == "__main__":
    main()
