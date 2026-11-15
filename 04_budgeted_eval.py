#!/usr/bin/env python3
"""Budgeted (top-K%) evaluation of the baseline uplift model.

Question: if sales ops can only contact K% of HCP-weeks, does targeting by modeled uplift beat
contacting K% at random with the best single channel?

Method: 5-fold GROUP cross-validation by HCP gives an out-of-fold uplift prediction for every randomized
row (no HCP is ever scored by a model that saw it). Each prediction comes from ONE model, like the
deployed single model (predictions are NOT averaged across models, which would evaluate an ensemble we
do not ship). Because results depend on how HCPs fall into folds, the whole procedure is repeated with
three different seeded fold assignments and we report each seed plus the mean. Policy value is the IPW
estimate (propensity 0.25); uncertainty is an HCP-cluster bootstrap within each seed.
Reuses the model code in 03_baseline_uplift.py (set USE_PARTD_FEATURES=1 to include benchmark features).

Run:  python3 -I 04_budgeted_eval.py
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("base", HERE / "03_baseline_uplift.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

PROP = base.PROP
BUDGETS = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00]
SEEDS = [42, 43, 44]
N_BOOT = 300
ARMS = ["none", "call", "email", "digital"]


def budget_table(df, oof, seed):
    elig = oof.copy()
    elig.columns = base.CHANNELS
    elig.loc[df["email_consent"].values == 0, "email"] = -np.inf
    best_ch, best_up = elig.idxmax(axis=1), elig.max(axis=1)
    order = (best_up.rank(ascending=False, method="first") / len(df)).values
    y, act = df.nbrx_2w.values, df.action.values
    agg = pd.DataFrame({"hcp": df.hcp_id.values, "n": 1})
    for a in ARMS:
        agg[f"y_{a}"] = y * (act == a)
    for K in BUDGETS:
        hit = ((order <= K) & (act == best_ch.values)) | ((order > K) & (act == "none"))
        agg[f"m_{K}"] = y * hit
    g = agg.groupby("hcp").sum()
    G, ix = g.to_numpy(), {c: i for i, c in enumerate(g.columns)}

    def stats(rows):
        s = G[rows].sum(axis=0)
        n = s[ix["n"]]
        v = {a: s[ix[f"y_{a}"]] / PROP / n for a in ARMS}
        out = {}
        for K in BUDGETS:
            out[K] = (s[ix[f"m_{K}"]] / PROP / n - v["none"], K * (v["call"] - v["none"]),
                      K * max(v[a] - v["none"] for a in ARMS[1:]))
        return out

    rng = np.random.default_rng(seed)
    point = stats(np.arange(len(G)))
    boots = [stats(rng.integers(0, len(G), len(G))) for _ in range(N_BOOT)]
    rows = []
    for K in BUDGETS:
        mg, rc, rb = point[K]
        d = np.array([b[K][0] - b[K][1] for b in boots])
        ci = np.percentile([b[K][0] for b in boots], [2.5, 97.5])
        rows.append({"seed": seed, "budget_pct": int(round(K * 100)), "model_gain": mg,
                     "model_ci_low": ci[0], "model_ci_high": ci[1], "random_call_gain": rc,
                     "random_best_channel_gain": rb, "model_minus_random_call": mg - rc,
                     "diff_ci_low": np.percentile(d, 2.5), "diff_ci_high": np.percentile(d, 97.5)})
    return pd.DataFrame(rows)


def main():
    cols = ", ".join(["hcp_id", "week", "action", "nbrx_2w"] + base.CAT + base.NUM)
    df = base.bq_csv(f"SELECT {cols} FROM nba.uplift_train WHERE is_randomized ORDER BY hcp_id, week")
    df = df.reset_index(drop=True)
    print(f"{len(df):,} randomized rows, {df.hcp_id.nunique():,} HCPs; features: {len(base.NUM) + len(base.CAT)}")

    per_seed = []
    for sd in SEEDS:
        oof = base.oof_uplift(df, repeats=1, seed=sd)
        per_seed.append(budget_table(df, oof, sd))
        print(f"  seed {sd} done")
    allr = pd.concat(per_seed, ignore_index=True)
    allr["significant"] = allr.diff_ci_low > 0

    mean = allr.groupby("budget_pct").agg(
        model_gain=("model_gain", "mean"), model_ci_low=("model_ci_low", "mean"), model_ci_high=("model_ci_high", "mean"),
        random_call_gain=("random_call_gain", "mean"), random_best_channel_gain=("random_best_channel_gain", "mean"),
        model_minus_random_call=("model_minus_random_call", "mean"),
        diff_ci_low=("diff_ci_low", "mean"), diff_ci_high=("diff_ci_high", "mean"),
        diff_min_across_seeds=("model_minus_random_call", "min"), diff_max_across_seeds=("model_minus_random_call", "max"),
        seeds_significant=("significant", "sum")).reset_index()
    mean["significant"] = mean.seeds_significant == len(SEEDS)
    mean = mean.round(4)

    print("\n=== Incremental NBRx per HCP-week over 'never touch' (mean of 3 independent CV runs) ===")
    print(mean[["budget_pct", "model_gain", "random_call_gain", "model_minus_random_call", "diff_ci_low",
                "diff_ci_high", "diff_min_across_seeds", "diff_max_across_seeds", "seeds_significant"]].to_string(index=False))

    out = HERE / "outputs"
    out.mkdir(exist_ok=True)
    mean.to_csv(out / "budget_curve.csv", index=False)
    allr.round(4).to_csv(out / "budget_curve_by_seed.csv", index=False)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 4.2))
        x = mean.budget_pct
        ax.plot(x, mean.model_gain, marker="o", label="Model (top-K by uplift), mean of 3 CV runs", color="#1f6f8b")
        ax.fill_between(x, mean.model_ci_low, mean.model_ci_high, alpha=0.2, color="#1f6f8b")
        for sd, grp in allr.groupby("seed"):
            ax.plot(grp.budget_pct, grp.model_gain, color="#1f6f8b", alpha=0.35, lw=0.8)
        ax.plot(x, mean.random_call_gain, marker="s", ls="--", label="Random K% with calls", color="#888")
        ax.plot(x, mean.random_best_channel_gain, marker="^", ls=":", label="Random K%, best single channel", color="#b56")
        ax.set_xlabel("Share of HCP-weeks contacted (%)")
        ax.set_ylabel("Incremental NBRx per HCP-week vs never touch")
        ax.set_title("Budgeted targeting: model vs random (out-of-fold)")
        ax.legend(frameon=False, fontsize=8)
        fig.tight_layout()
        fig.savefig(out / "budget_curve.png", dpi=160)
        print("Saved outputs/budget_curve.png")
    except Exception as e:
        print("Plot skipped:", e)


if __name__ == "__main__":
    main()
