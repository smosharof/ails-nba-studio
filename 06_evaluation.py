#!/usr/bin/env python3
"""Final evaluation suite (matches the proposal's Evaluation section).

1. Actual vs predicted lift  -- out-of-fold, top-30% contact budget, IPW, HCP-cluster bootstrap
2. Segment stability         -- (a) over time: model trained on weeks 1-13 vs 14-26, rank agreement
                                (b) across segments: gain at the 30% budget per segment
3. Leakage checks            -- (a) panel features use 2025 months only (verified against raw)
                                (b) permutation test: shuffled outcomes must give ~zero gain

Writes outputs/evaluation.json and prints a readable report.
Run:  python3 -I 06_evaluation.py
"""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.model_selection import GroupKFold

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("base", HERE / "03_baseline_uplift.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
common, SEED, PROP = base.common, base.SEED, base.PROP
K = 0.30
N_BOOT = 300


def oof_predictions(df):
    return base.oof_uplift(df, repeats=1)


def select(df, oof):
    elig = oof.copy()
    elig.columns = base.CHANNELS
    elig.loc[df["email_consent"].values == 0, "email"] = -np.inf
    ch, up = elig.idxmax(axis=1), elig.max(axis=1)
    rank = up.rank(ascending=False, method="first") / len(df)
    return ch, up, (rank <= K) & (up > 0)


def realized_and_predicted(df, ch, up, sel):
    """Per-HCP sums so we can bootstrap by HCP: realized effect (IPW) of chosen channel vs none."""
    y, act = df.nbrx_2w.values, df.action.values
    real = (((act == ch.values) * y) - ((act == "none") * y)) / PROP
    t = pd.DataFrame({"hcp": df.hcp_id.values, "n_sel": sel.values.astype(int),
                      "real": real * sel.values, "pred": up.values * sel.values})
    return t.groupby("hcp").sum()


def boot_ratio(g, rng):
    G = g[["n_sel", "real", "pred"]].to_numpy()
    def f(ix):
        s = G[ix].sum(axis=0)
        return s[1] / s[0], s[2] / s[0]
    pt = f(np.arange(len(G)))
    bs = np.array([f(rng.integers(0, len(G), len(G))) for _ in range(N_BOOT)])
    return pt, np.percentile(bs[:, 0], [2.5, 97.5])


def main():
    cols = ", ".join(["hcp_id", "week", "action", "nbrx_2w"] + base.CAT + base.NUM + ["growth_group"])
    df = common.bq_df(f"SELECT {cols} FROM nba.uplift_train WHERE is_randomized ORDER BY hcp_id, week").reset_index(drop=True)
    rng = np.random.default_rng(SEED)
    out = {}

    # ---- 1. actual vs predicted lift ---------------------------------------------------
    oof = oof_predictions(df)
    ch, up, sel = select(df, oof)
    g = realized_and_predicted(df, ch, up, sel)
    (real, pred), ci = boot_ratio(g, rng)
    out["actual_vs_predicted"] = {
        "budget": K, "n_selected_hcp_weeks": int(sel.sum()),
        "predicted_uplift_per_contact": round(float(pred), 4),
        "actual_uplift_per_contact": round(float(real), 4),
        "actual_ci95": [round(float(ci[0]), 4), round(float(ci[1]), 4)],
        "calibration_ratio_actual_over_predicted": round(float(real / pred), 3)}
    print("\n[1] Actual vs predicted lift (top 30% budget, out-of-fold)")
    print(json.dumps(out["actual_vs_predicted"], indent=2))

    # ---- 2a. stability over time -----------------------------------------------------------
    weeks = sorted(df.week.unique())
    mid = weeks[len(weeks) // 2 - 1]
    early, late = df[df.week <= mid], df[df.week > mid]
    latest = common.bq_df("SELECT * FROM nba.uplift_train WHERE week = (SELECT MAX(week) FROM nba.uplift_train)"
                          ).drop_duplicates("hcp_id").reset_index(drop=True)
    ups = {}
    for name, part in [("early", early), ("late", late)]:
        m, e = base.fit_tlearner(part.reset_index(drop=True))
        ups[name] = base.predict_uplift(m, e, latest)
    stab = {}
    for c in base.CHANNELS:
        a, b = ups["early"][f"uplift_{c}"], ups["late"][f"uplift_{c}"]
        top = lambda s: set(s.nlargest(int(0.2 * len(s))).index)  # noqa: E731
        ta, tb = top(a), top(b)
        stab[c] = {"spearman": round(float(spearmanr(a, b)[0]), 3),
                   "top20_overlap_jaccard": round(len(ta & tb) / len(ta | tb), 3)}
    out["stability_over_time"] = {"split_week": mid, "by_channel": stab}
    print(f"\n[2a] Stability over time (model on weeks <= {mid} vs after)")
    print(json.dumps(stab, indent=2))

    # ---- 2b. stability across segments -----------------------------------------------------
    seg_rows = []
    for seg, idx in df.groupby("segment").groups.items():
        sub = df.loc[idx]
        gs = realized_and_predicted(sub, ch.loc[idx], up.loc[idx], sel.loc[idx])
        if gs.n_sel.sum() >= 100:
            (r_, p_), ci_ = boot_ratio(gs, rng)
            seg_rows.append({"segment": seg, "n_contacts": int(gs.n_sel.sum()),
                             "actual_uplift": round(float(r_), 3), "ci95_low": round(float(ci_[0]), 3),
                             "ci95_high": round(float(ci_[1]), 3)})
    out["stability_across_segments"] = seg_rows
    print("\n[2b] Realized uplift per contact by segment (top 30% budget)")
    print(pd.DataFrame(seg_rows).to_string(index=False))

    # ---- 3a. leakage: panel features must use 2025 only --------------------------------------
    chk = common.bq_query("""
      WITH p25 AS (SELECT hcp_id, AVG(glucoren_trx) a25 FROM nba.raw_hcp_monthly WHERE month <= '2025-12' GROUP BY 1),
           pall AS (SELECT hcp_id, AVG(glucoren_trx) aall FROM nba.raw_hcp_monthly GROUP BY 1)
      SELECT MAX(ABS(f.h_glucoren_trx_mo - p25.a25)) max_diff_vs_2025_only,
             AVG(ABS(f.h_glucoren_trx_mo - pall.aall)) mean_diff_vs_all_months
      FROM nba.features f JOIN p25 USING (hcp_id) JOIN pall USING (hcp_id)""")[0]
    leak_ok = chk["max_diff_vs_2025_only"] < 1e-6 and chk["mean_diff_vs_all_months"] > 0
    out["leakage_panel_check"] = {**{k: round(v, 6) for k, v in chk.items()}, "pass": bool(leak_ok)}
    print("\n[3a] Panel-feature leakage check:", out["leakage_panel_check"])

    # ---- 3b. permutation test --------------------------------------------------------------
    shuf = df.copy()
    shuf["nbrx_2w"] = rng.permutation(shuf["nbrx_2w"].values)
    oof_s = oof_predictions(shuf)
    ch_s, up_s, sel_s = select(shuf, oof_s)
    gs = realized_and_predicted(shuf, ch_s, up_s, sel_s)
    (r_s, _), ci_s = boot_ratio(gs, rng)
    out["leakage_permutation_test"] = {
        "realized_uplift_with_shuffled_outcomes": round(float(r_s), 4),
        "ci95": [round(float(ci_s[0]), 4), round(float(ci_s[1]), 4)],
        "pass": bool(ci_s[0] <= 0 <= ci_s[1])}
    print("\n[3b] Permutation test (outcomes shuffled; expect ~0):", out["leakage_permutation_test"])

    (HERE / "outputs").mkdir(exist_ok=True)
    (HERE / "outputs" / "evaluation.json").write_text(json.dumps(out, indent=2))
    print("\nSaved outputs/evaluation.json")


if __name__ == "__main__":
    main()
