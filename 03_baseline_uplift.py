#!/usr/bin/env python3
"""Baseline NBA model: KMeans segmentation + T-learner uplift per channel.

Trains ONLY on the randomized exploration arm (is_randomized = TRUE), where each
action had propensity 0.25, so uplift estimates are unbiased. Evaluates on
held-out HCPs (group split -- no HCP appears in both train and test) using
inverse-propensity-weighted (IPW) policy value, then refits on all randomized
rows and writes recommendations to nba.recommendations.

Run:  python3 -I 03_baseline_uplift.py        (needs gcloud + bq on PATH)
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import OrdinalEncoder, StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

PROJECT = "fall26-ails-termproj-nba"
OUT = Path(__file__).resolve().parent / "outputs"
OUT.mkdir(exist_ok=True)
SEED = 42
MODEL_VERSION = os.environ.get("MODEL_VERSION", "baseline-tlearner-v1")
TRAIN_MAX_WEEK = os.environ.get("TRAIN_MAX_WEEK")  # e.g. 2026-06-01; default = all weeks
SKIP_EVAL = os.environ.get("SKIP_EVAL") == "1"
CHANNELS = ["call", "email", "digital"]
PROP = 0.25  # known randomization probability per action in the explore arm

CAT = ["last_action", "specialty", "region", "practice_setting", "segment", "growth_group"]
NUM = [
    "calls_4w", "emails_4w", "digital_4w", "weeks_since_call", "trailing_nbrx_wk",
    "years_in_practice", "pct_medicare", "pct_commercial", "pct_medicaid",
    "mean_patient_age", "pct_cvd_comorbid", "glucoren_reject_rate", "decile",
    "email_consent", "h_market_trx_mo", "h_glucoren_trx_mo", "h_glucoren_nbrx_mo",
    "h_glucoren_share", "h_competitor_share", "h_net_switch", "h_rep_calls_mo",
    "h_samples_mo", "h_emails_sent_mo", "h_email_open_rate", "h_web_visits_mo",
    "aff_rep_call", "aff_remote_call", "aff_email", "aff_digital", "aff_webinar",
    "aff_conference",
]
# Real CMS Part D benchmark features (08/10 scripts). TESTED and left OFF by default: adding them did not
# improve out-of-fold budgeted gains (see outputs/*_with_partd_features.*). Enable with USE_PARTD_FEATURES=1.
if os.environ.get("USE_PARTD_FEATURES") == "1":
    NUM = NUM + ["bm_p50_clms", "bm_p90_clms", "hcp_vs_bm"]
# Deliberately EXCLUDED: engaged, complaint (post-treatment outcomes), propensity
# (constant in the explore arm), state (high cardinality), nbrx_2w (the target).


def bq_csv(sql: str) -> pd.DataFrame:
    return common.bq_df(sql)


def make_matrix(df, enc=None):
    X = df[NUM].copy()
    cats = df[CAT].astype("string").fillna("missing")
    if enc is None:
        enc = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1).fit(cats)
    X[CAT] = enc.transform(cats)
    return X, enc


def new_model():
    cat_mask = [c in CAT for c in NUM + CAT]
    return HistGradientBoostingRegressor(
        max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=100,
        l2_regularization=1.0, categorical_features=cat_mask, random_state=SEED)


def fit_tlearner(train):
    X, enc = make_matrix(train)
    models = {}
    for a in CHANNELS + ["none"]:
        m = train["action"] == a
        models[a] = new_model().fit(X[m.values], train.loc[m, "nbrx_2w"])
    return models, enc


def predict_uplift(models, enc, df):
    X, _ = make_matrix(df, enc)
    base = models["none"].predict(X)
    return pd.DataFrame({f"uplift_{c}": models[c].predict(X) - base for c in CHANNELS},
                        index=df.index)


def oof_uplift(df, repeats=3, folds=5, seed=SEED):
    """Out-of-fold uplift for EVERY row: HCPs are split into folds (never scored by a model that saw them).
    Repeated with different seeded fold assignments and averaged, so results do not hinge on one split."""
    hcps = df.hcp_id.unique()
    acc = np.zeros((len(df), len(CHANNELS)))
    for r in range(repeats):
        rng = np.random.default_rng(seed + r)
        fold_of = dict(zip(hcps, rng.permutation(len(hcps)) % folds))
        fold = df.hcp_id.map(fold_of).values
        for k in range(folds):
            te = np.where(fold == k)[0]
            tr = np.where(fold != k)[0]
            m, e = fit_tlearner(df.iloc[tr].reset_index(drop=True))
            acc[te] += predict_uplift(m, e, df.iloc[te].reset_index(drop=True)).values
    return pd.DataFrame(acc / repeats, index=df.index, columns=[f"uplift_{c}" for c in CHANNELS])


def choose(up, consent):
    """Argmax over eligible channels; 'none' if nothing beats zero. Email needs consent."""
    u = up[[f"uplift_{c}" for c in CHANNELS]].copy()
    u.columns = CHANNELS
    u.loc[consent.values == 0, "email"] = -np.inf
    best = u.idxmax(axis=1)
    return best.where(u.max(axis=1) > 0, "none"), u.max(axis=1).clip(lower=0)


def ipw_value(test, policy):
    """IPW estimate of mean nbrx_2w if `policy` (Series of actions) were applied."""
    hit = (test["action"].values == policy.values)
    return float((hit * test["nbrx_2w"].values).sum() / PROP / len(test))


def evaluate(df):
    """Held-out (group split by HCP) policy value + calibration. Returns (policy_df, calibration_df)."""
    # ---- group split by HCP -------------------------------------------------
    rng = np.random.default_rng(SEED)
    hcps = df.hcp_id.unique()
    test_h = set(rng.choice(hcps, size=int(0.3 * len(hcps)), replace=False))
    is_test = df.hcp_id.isin(test_h)
    train, test = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)
    print(f"  train {len(train):,} rows / test {len(test):,} rows (split by HCP)")

    models, enc = fit_tlearner(train)
    up = predict_uplift(models, enc, test)
    test = pd.concat([test, up], axis=1)

    # ---- evaluation: policy value (IPW) with HCP-cluster bootstrap ---------------
    pol_model, _ = choose(up, test["email_consent"])
    policies = {
        "model (argmax uplift, consent-aware)": pol_model,
        "always call": pd.Series("call", index=test.index),
        "always email": pd.Series("email", index=test.index),
        "always digital": pd.Series("digital", index=test.index),
        "never touch": pd.Series("none", index=test.index),
    }
    rows = []
    hcp_groups = {h: g.index.values for h, g in test.groupby("hcp_id")}
    keys = np.array(list(hcp_groups))
    boots = {k: [] for k in policies}
    for _ in range(200):
        pick = rng.choice(keys, size=len(keys), replace=True)
        idx = np.concatenate([hcp_groups[h] for h in pick])
        bt = test.loc[idx].reset_index(drop=True)
        for k, p in policies.items():
            boots[k].append(ipw_value(bt, p.loc[idx].reset_index(drop=True)))
    random_val = float(test["nbrx_2w"].mean())  # uniform-random policy value
    for k, p in policies.items():
        v = ipw_value(test, p)
        lo, hi = np.percentile(boots[k], [2.5, 97.5])
        rows.append({"policy": k, "value_nbrx_2w": round(v, 4),
                     "ci95_low": round(lo, 4), "ci95_high": round(hi, 4),
                     "vs_random": round(v - random_val, 4)})
    pol = pd.DataFrame(rows)
    # paired bootstrap: model minus best single channel
    best_single = max(["always call", "always email", "always digital"],
                      key=lambda k: pol.set_index("policy").loc[k, "value_nbrx_2w"])
    diff = np.array(boots["model (argmax uplift, consent-aware)"]) - np.array(boots[best_single])
    print("\n=== Policy value on held-out HCPs (IPW, mean NBRx per HCP-week) ===")
    print(pol.to_string(index=False))
    print(f"\nModel minus best single channel ({best_single}): "
          f"{diff.mean():+.4f}  95% CI [{np.percentile(diff, 2.5):+.4f}, {np.percentile(diff, 97.5):+.4f}]")
    print(f"Uniform-random policy value: {random_val:.4f}")

    # ---- calibration: observed lift by predicted-uplift quintile ---------------
    cal_rows = []
    for c in CHANNELS:
        q = pd.qcut(test[f"uplift_{c}"], 5, labels=False, duplicates="drop")
        for b in sorted(q.dropna().unique()):
            s = test[q == b]
            t, k = s[s.action == c], s[s.action == "none"]
            if len(t) > 30 and len(k) > 30:
                cal_rows.append({"channel": c, "quintile": int(b) + 1,
                                 "pred_uplift": round(s[f"uplift_{c}"].mean(), 4),
                                 "obs_lift": round(t.nbrx_2w.mean() - k.nbrx_2w.mean(), 4),
                                 "n_treated": len(t), "n_control": len(k)})
    cal = pd.DataFrame(cal_rows)
    print("\n=== Calibration: predicted vs observed lift by quintile (1=lowest) ===")
    print(cal.to_string(index=False))
    return pol, cal


def score_and_write(df, model_version=None, pol=None, cal=None, write_files=True):
    """Refit on all rows in df, score the latest week for every HCP, write nba.recommendations."""
    model_version = model_version or MODEL_VERSION
    # ---- refit on ALL randomized rows, score latest week for every HCP -------------
    print("\nRefitting on all randomized rows and scoring ...")
    models, enc = fit_tlearner(df)
    latest = bq_csv("SELECT * FROM nba.uplift_train "
                    "QUALIFY ROW_NUMBER() OVER (PARTITION BY hcp_id ORDER BY week DESC) = 1 ORDER BY hcp_id")
    latest = latest.drop_duplicates("hcp_id").reset_index(drop=True)
    up = predict_uplift(models, enc, latest)
    rec_ch, rec_up = choose(up, latest["email_consent"])

    # ---- segmentation (KMeans, k=5) ----------------------------------------------------
    seg_cols = ["h_glucoren_share", "h_market_trx_mo", "trailing_nbrx_wk",
                "pct_medicare", "years_in_practice", "glucoren_reject_rate"]
    Z = SimpleImputer(strategy="median").fit_transform(latest[seg_cols])
    Z = StandardScaler().fit_transform(Z)
    cluster = KMeans(n_clusters=5, n_init=10, random_state=SEED).fit_predict(Z)

    rec = pd.DataFrame({
        "hcp_id": latest.hcp_id,
        "scoring_week": latest.week,
        "cluster": cluster,
        "uplift_call": up.uplift_call.round(5),
        "uplift_email": up.uplift_email.round(5),
        "uplift_digital": up.uplift_digital.round(5),
        "email_consent": latest.email_consent.astype(int),
        "recommended_channel": rec_ch.values,
        "expected_uplift": rec_up.round(5).values,
        "model_version": model_version,
        "scored_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    })
    rec["priority_rank"] = rec.expected_uplift.rank(ascending=False, method="first").astype(int)
    rec = rec.sort_values("priority_rank")

    if write_files:   # local CSV copies; the workflow/demo turns this off so it never touches these files
        if pol is not None:
            pol.to_csv(OUT / "policy_value.csv", index=False)
        if cal is not None:
            cal.to_csv(OUT / "calibration.csv", index=False)
        rec.to_csv(OUT / "recommendations.csv", index=False)
    print("\nRecommendation mix:\n", rec.recommended_channel.value_counts().to_string())

    common.bq_load_df(rec, "recommendations")
    print(f"Wrote {len(rec):,} rows to nba.recommendations" + (" and outputs/ CSVs." if write_files else "."))
    return rec


def main():
    print("Loading randomized rows from BigQuery ...")
    cols = ", ".join(["hcp_id", "week", "action", "nbrx_2w"] + CAT + NUM)
    wk = f" AND week <= '{TRAIN_MAX_WEEK}'" if TRAIN_MAX_WEEK else ""
    df = bq_csv(f"SELECT {cols} FROM nba.uplift_train WHERE is_randomized{wk} ORDER BY hcp_id, week")
    print(f"  {len(df):,} rows, {df.hcp_id.nunique():,} HCPs (train through {TRAIN_MAX_WEEK or 'all weeks'})")
    pol = cal = None
    if not SKIP_EVAL:
        pol, cal = evaluate(df)
    score_and_write(df, MODEL_VERSION, pol, cal)


if __name__ == "__main__":
    sys.exit(main())
