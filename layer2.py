#!/usr/bin/env python3
"""Layer 2: scoring layer on top of the uplift model.

Reads nba.recommendations (Layer 1 output) and each HCP's latest contact history and consent, then:
  1. enforces frequency caps per HCP,
  2. applies channel-substitution rules (fall to next-best eligible channel),
  3. keeps only the top BUDGET_PCT of HCPs as 'contact' (uplift is only
     trustworthy for the high-uplift tail -- see the budgeted evaluation),
  4. attaches a rationale + pre-approved message, and runs the compliance filter (fails closed),
  5. publishes nba.recommendations_final and the view nba.v_recommendations.

The work is split so a DRY RUN is possible:
  compute()  -> builds the full output in memory (no writes)
  diff()     -> compares a computed output with what is currently published
  publish()  -> writes the output
  run()      -> compute + publish (what the pipeline calls)
The workflow's "Layer 2 gate" uses compute() + diff(): Layer 2 runs only if the dry run would change
something, so what will happen is known in advance.

Run:  python3 -I layer2.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import compliance  # noqa: E402

CHANNELS = ["call", "email", "digital"]
BUDGET_PCT = 0.30          # contact at most 30% of HCPs (significant region of the budget curve)
CAPS = {"call": 2, "email": 2, "digital": 3}   # max touches per rolling 4 weeks
MIN_UPLIFT = 0.0           # never contact on non-positive expected uplift

OUT_COLS = ["hcp_id", "scoring_week", "model_version", "cluster", "segment", "priority_rank",
            "contact_flag", "model_channel", "final_channel", "substituted_from", "blocked_reason",
            "final_uplift", "uplift_call", "uplift_email", "uplift_digital", "rationale",
            "message_guidance", "evidence_pmid", "evidence_title", "compliance_ok",
            "compliance_note", "generated_at"]
# Everything a downstream user could see change (generated_at is excluded on purpose).
COMPARE_COLS = [c for c in OUT_COLS if c not in ("hcp_id", "generated_at", "evidence_title")]


def blocked_channels(row):
    """Return {channel: reason} for channels that may not be used this week."""
    b = {}
    if row.calls_4w >= CAPS["call"]:
        b["call"] = f"frequency cap ({int(row.calls_4w)} calls in last 4 wks >= {CAPS['call']})"
    if row.email_consent != 1:
        b["email"] = "no email consent"
    elif row.emails_4w >= CAPS["email"]:
        b["email"] = f"frequency cap ({int(row.emails_4w)} emails in last 4 wks >= {CAPS['email']})"
    elif row.last_action == "email":
        b["email"] = "no back-to-back emails (complaint risk)"
    if row.digital_4w >= CAPS["digital"]:
        b["digital"] = f"frequency cap ({int(row.digital_4w)} digital in last 4 wks >= {CAPS['digital']})"
    return b


def load_overrides():
    """Demo-only overrides (nba.demo_overrides). Returns {} when the table is absent or empty."""
    try:
        rows = common.bq_query("SELECT key, value FROM nba.demo_overrides")
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            pass
    return out


def read_inputs():
    """Layer 1 output + each HCP's LATEST contact history and consent (latest row per HCP, not latest week)."""
    rec = common.bq_df("SELECT * FROM nba.recommendations ORDER BY hcp_id")
    rec = rec.drop(columns=["email_consent"], errors="ignore")      # use the fresh value below
    hist = common.bq_df(
        "SELECT hcp_id, calls_4w, emails_4w, digital_4w, last_action, weeks_since_call, segment, email_consent "
        "FROM nba.uplift_train QUALIFY ROW_NUMBER() OVER (PARTITION BY hcp_id ORDER BY week DESC) = 1")
    return rec.merge(hist, on="hcp_id", how="left").sort_values("hcp_id").reset_index(drop=True)


def compute(budget_pct: float = BUDGET_PCT, refresh_reference: bool = False, overrides=None):
    """Build the full Layer 2 output in memory. Returns (out_df, meta). Writes nothing."""
    ref = compliance.fetch_public_reference(force=refresh_reference)
    claim = ref["approved_claim"]
    evid = ref.get("evidence", [])
    overrides = load_overrides() if overrides is None else overrides
    claim_ov = overrides.get("claim_override")          # {"channel": ..., "claim": ...} (demo only)

    df = read_inputs()
    n = len(df)

    final_ch, reasons, subst_from, final_up = [], [], [], []
    for r in df.itertuples():
        blocked = blocked_channels(r)
        ups = {c: getattr(r, f"uplift_{c}") for c in CHANNELS}
        elig = {c: u for c, u in ups.items() if c not in blocked and u > MIN_UPLIFT}
        best = max(elig, key=elig.get) if elig else "none"
        final_ch.append(best)
        final_up.append(elig[best] if best != "none" else 0.0)
        orig = r.recommended_channel
        if orig != "none" and orig in blocked:
            subst_from.append(orig)
            reasons.append(f"{orig} blocked: {blocked[orig]}" +
                           (f" -> substituted {best}" if best != "none" else " -> no eligible channel"))
        else:
            subst_from.append(None)
            reasons.append(None)
    df["model_channel"] = df.recommended_channel
    df["final_channel"] = final_ch
    df["final_uplift"] = np.round(final_up, 5)
    df["substituted_from"] = subst_from
    df["blocked_reason"] = reasons

    # Budget: keep only the top share of HCPs by final expected uplift.
    df["priority_rank"] = df.final_uplift.rank(ascending=False, method="first").astype(int)
    cutoff = int(round(budget_pct * n))
    over = (df.priority_rank > cutoff) & (df.final_channel != "none")
    df.loc[over, "blocked_reason"] = df.loc[over, "blocked_reason"].fillna("") + \
        f" outside top {int(budget_pct * 100)}% contact budget"
    df["contact_flag"] = (df.priority_rank <= cutoff) & (df.final_channel != "none")
    df.loc[~df.contact_flag, "final_channel"] = np.where(df.loc[~df.contact_flag, "final_channel"] == "none",
                                                          "none", "none (below budget)")

    # Rationale, pre-approved message, evidence, compliance check.
    rationale, msg, pmid, etitle, ok, note = [], [], [], [], [], []
    for r in df.itertuples():
        e = evid[int(r.cluster) % len(evid)] if evid else {"pmid": "", "title": ""}
        if r.contact_flag:
            pct = int(np.ceil(100 * r.priority_rank / n))
            rt = (f"Recommend {r.final_channel}: top {pct}% of HCPs by estimated response uplift "
                  f"(score {r.final_uplift:.2f}; the ranking is informative but only moderately stable, and the absolute size is overstated). "
                  f"Segment: {r.segment}. "
                  f"Comparator-class context, not GLUCOREN-specific (PubMed {e['pmid']}): {e['title']}.")
            # DEMO ONLY: an unsafe wording override for one channel, to show the compliance filter failing closed.
            use = claim_ov["claim"] if claim_ov and r.final_channel == claim_ov["channel"] else claim
            mt = compliance.talking_point(use, r.final_channel)
            good, nt = compliance.check_text(mt, claim)          # always checked against the APPROVED claim
            good2, nt2 = compliance.check_text(rt, claim, require_claim=False)
            ok.append(bool(good and good2))
            note.append("; ".join(x for x in [nt if not good else "", nt2 if not good2 else ""] if x) or "pass")
        else:
            why = r.blocked_reason if isinstance(r.blocked_reason, str) and r.blocked_reason.strip() else None
            rt = "No contact this period: " + (why or "no channel with positive expected uplift")
            mt, good, nt = "", True, "n/a (no message)"
            ok.append(True)
            note.append(nt)
        rationale.append(rt.strip())
        msg.append(mt)
        pmid.append(e["pmid"] if r.contact_flag else "")
        etitle.append(e["title"] if r.contact_flag else "")
    df["rationale"], df["message_guidance"] = rationale, msg
    df["evidence_pmid"], df["evidence_title"] = pmid, etitle
    df["compliance_ok"], df["compliance_note"] = ok, note
    held = ~df.compliance_ok & df.contact_flag                   # fail closed: withhold the contact
    df.loc[held, "contact_flag"] = False
    df.loc[held, "final_channel"] = "none (compliance hold)"
    df["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    out = df.sort_values("priority_rank")[OUT_COLS].reset_index(drop=True)
    meta = {"claim_source": ref.get("claim_source"), "claim_override_active": bool(claim_ov),
            "claim_override_channel": claim_ov["channel"] if claim_ov else None}
    return out, meta


def publish(out: pd.DataFrame):
    """Write recommendations_final and refresh the view that Agent 2 reads."""
    common.bq_load_df(out, "recommendations_final")
    common.bq_query(
        "CREATE OR REPLACE VIEW nba.v_recommendations AS SELECT hcp_id, priority_rank, contact_flag, "
        "final_channel, model_channel, substituted_from, blocked_reason, final_uplift, cluster, segment, "
        "rationale, message_guidance, evidence_pmid, compliance_ok, model_version, scoring_week "
        "FROM nba.recommendations_final")


def summarize(out: pd.DataFrame, meta=None):
    meta = meta or {}
    held = out.final_channel.eq("none (compliance hold)")
    return {
        "hcps": int(len(out)), "model_version": str(out.model_version.iloc[0]),
        "contacts": int(out.contact_flag.sum()),
        "mix": out[out.contact_flag].final_channel.value_counts().to_dict(),
        "substituted": int(out.substituted_from.notna().sum()),
        "blocked_by_cap_or_consent": int(out.blocked_reason.fillna("").str.contains("cap|consent|back-to-back").sum()),
        "compliance_failures": int((~out.compliance_ok).sum()),
        "contacts_withheld_by_compliance": int(held.sum()),
        "claim_source": meta.get("claim_source"),
        "claim_override_active": meta.get("claim_override_active", False),
    }


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    d = df.set_index("hcp_id")[COMPARE_COLS].copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].round(5)
    d = d.fillna("").astype(str)
    # BigQuery auto-detects PubMed IDs as numbers, so they can come back as '23494446.0'
    d["evidence_pmid"] = d["evidence_pmid"].str.replace(r"\.0$", "", regex=True)
    return d.sort_index()


def read_published():
    try:
        return common.bq_df("SELECT * FROM nba.recommendations_final")
    except Exception:
        return pd.DataFrame()


def diff(published: pd.DataFrame, new: pd.DataFrame, n_examples: int = 8):
    """What would change if `new` replaced `published`? Deterministic and exact."""
    if published is None or len(published) == 0:
        return {"changed_hcps": int(len(new)), "total": int(len(new)), "publish_needed": True,
                "layer1_changed": int(len(new)), "channel_or_contact_changed": int(len(new)),
                "message_changed": 0, "newly_blocked_by_compliance": 0, "examples": [],
                "note": "nothing is published yet"}
    a, b = _norm(published), _norm(new)
    common_idx = a.index.intersection(b.index)
    a, b = a.loc[common_idx], b.loc[common_idx]
    changed = (a != b).any(axis=1)
    l1_cols = ["model_version", "model_channel", "uplift_call", "uplift_email", "uplift_digital", "scoring_week"]
    layer1 = (a[l1_cols] != b[l1_cols]).any(axis=1)
    chan = (a.final_channel != b.final_channel) | (a.contact_flag != b.contact_flag)
    msg = a.message_guidance != b.message_guidance
    newly_blocked = (a.compliance_ok == "True") & (b.compliance_ok == "False")
    ex = []
    newd = new.set_index("hcp_id")
    for hid in b[changed & (chan | msg | newly_blocked)].sort_values("priority_rank").index[:n_examples]:
        ex.append({"hcp_id": hid, "before": a.loc[hid, "final_channel"], "after": b.loc[hid, "final_channel"],
                   "why": (newd.loc[hid, "blocked_reason"] if isinstance(newd.loc[hid, "blocked_reason"], str) and
                           newd.loc[hid, "blocked_reason"].strip() else newd.loc[hid, "compliance_note"])})
    n_changed = int(changed.sum())
    return {"changed_hcps": n_changed, "total": int(len(b)), "publish_needed": n_changed > 0 or len(a) != len(new),
            "layer1_changed": int(layer1.sum()), "channel_or_contact_changed": int(chan.sum()),
            "message_changed": int(msg.sum()), "newly_blocked_by_compliance": int(newly_blocked.sum()),
            "score_or_text_only_changed": int((changed & ~chan & ~msg).sum()), "examples": ex}


def run(budget_pct: float = BUDGET_PCT, refresh_reference: bool = False):
    """Compute and publish (the pipeline entry point). Returns the summary dict."""
    out, meta = compute(budget_pct, refresh_reference)
    publish(out)
    return summarize(out, meta)


if __name__ == "__main__":
    print(run())
