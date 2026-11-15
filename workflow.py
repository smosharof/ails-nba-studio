#!/usr/bin/env python3
"""Agent 1 workflow: CODE decides, the LLM explains.

The sequence and every decision are deterministic, so what will happen is known in advance:

  1. refresh   rebuild features from the raw tables (picks up changed source data)
  2. freshness compare the model's training cutoff with the latest outcome week
               GATE 1: >= MIN_NEW_WEEKS new outcome weeks -> RETRAIN, otherwise HOLD
  3. layer 1   (only if RETRAIN) test the old model on the unseen weeks, then retrain
  4. layer 2 gate
               GATE 2: run Layer 2 in memory (dry run) and diff it against what is published.
               Anything would change -> RUN, nothing would change -> SKIP
  5. layer 2   publish (only if RUN)
  6. verify    compliance failures, consistency with Layer 1, contact budget, label cross-check
  7. report    plain-English summary

The LLM (Gemini) never chooses a step. It explains each step in plain English from the facts, and writes
the closing report, flagging concerns. If it is unavailable the deterministic reasons are used instead.

Two modes: 'auto' (scheduled job) and 'stepper' (GUI: pauses at the gates so you can inject demo data,
re-check the gate, and continue).  Every step is also written to nba.run_log.

CLI:  python3 -I workflow.py run [--no-narrate]
"""
import importlib.util
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
import layer2  # noqa: E402

_spec = importlib.util.spec_from_file_location("base", HERE / "03_baseline_uplift.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

AGENT = "agent1-workflow"
MIN_NEW_WEEKS = 2                       # Gate 1 rule
ORIGINAL_MAX_WEEK = "2026-06-29"        # last week of the original course data
BASELINE_VERSION = "v2-thru-2026-06-29"  # the production model version the demo resets to
WAIT_TIMEOUT_S = 20 * 60                # a paused run is abandoned after this long


# ------------------------------------------------------------------------------------------- facts
def get_state():
    """Freshness facts: latest outcome week vs the model's training cutoff."""
    wk = common.bq_query("SELECT CAST(MAX(week) AS STRING) AS latest, COUNT(DISTINCT week) AS weeks "
                         "FROM nba.raw_touch_log WHERE nbrx_2w IS NOT NULL")[0]
    cur = common.bq_query("SELECT model_version, CAST(MAX(scoring_week) AS STRING) AS scoring_week, COUNT(*) AS hcps "
                          "FROM nba.recommendations GROUP BY 1")[0]
    m = re.search(r"thru-(\d{4}-\d{2}-\d{2})", cur["model_version"] or "")
    cutoff = m.group(1) if m else None
    new_weeks = None
    if cutoff:
        new_weeks = common.bq_query(
            f"SELECT COUNT(DISTINCT week) AS n FROM nba.raw_touch_log "
            f"WHERE nbrx_2w IS NOT NULL AND week > DATE '{cutoff}'")[0]["n"]
    return {"latest_outcome_week": wk["latest"], "outcome_weeks_total": wk["weeks"],
            "layer1_model_version": cur["model_version"], "model_cutoff": cutoff,
            "new_outcome_weeks": new_weeks}


def snapshot():
    """Published Layer 2 state, for before/after comparison."""
    r = common.bq_query(
        "SELECT ANY_VALUE(model_version) AS model_version, COUNTIF(contact_flag) AS contacts, "
        "COUNTIF(contact_flag AND final_channel='call') AS call, COUNTIF(contact_flag AND final_channel='email') AS email, "
        "COUNTIF(contact_flag AND final_channel='digital') AS digital, COUNTIF(substituted_from IS NOT NULL) AS substituted, "
        "COUNTIF(NOT compliance_ok) AS compliance_failures, COUNTIF(final_channel='none (compliance hold)') AS withheld, "
        "COUNT(*) AS hcps FROM nba.recommendations_final")[0]
    return r


def recs_status(src, pub, g2):
    """Pure rule behind the 'recommendations' status line. Times are 'YYYY-MM-DD HH:MM:SS' strings (UTC) or None.

    The recommendations are pending when a source table Layer 2 reads (raw_* tables, wording overrides) changed after
    BOTH the last publish and the last time Layer 2's gate looked at it (a gate that decided SKIP also counts as looked)."""
    if not src or not pub:
        return "unknown"
    return "pending" if src > max(pub, g2 or "") else "current"


def recs_freshness():
    """Are the published recommendations behind the source data? A cheap metadata check; it does NOT recompute Layer 2."""
    fmt = "FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', {})"
    t = "`fall26-ails-termproj-nba.nba.__TABLES__`"
    try:
        r = common.bq_query(
            "SELECT " + fmt.format(f"(SELECT TIMESTAMP_MILLIS(MAX(last_modified_time)) FROM {t} "
                                   "WHERE STARTS_WITH(table_id, 'raw_') OR table_id = 'demo_overrides')") + " AS src, "
            + fmt.format(f"(SELECT TIMESTAMP_MILLIS(last_modified_time) FROM {t} WHERE table_id = 'recommendations_final')") + " AS pub, "
            + fmt.format("(SELECT MAX(run_ts) FROM `fall26-ails-termproj-nba.nba.run_log` "
                         f"WHERE agent = '{AGENT}' AND step = 'layer2_gate')") + " AS g2")[0]
        return {"status": recs_status(r["src"], r["pub"], r["g2"]), "published_at": r["pub"], "source_changed_at": r["src"]}
    except Exception:
        return {"status": "unknown", "published_at": None, "source_changed_at": None}


# Tables that exist in the dataset but are never shown or previewed in the console (audit trail and demo bookkeeping).
HIDDEN_TABLES = {"run_log", "demo_changes", "demo_overrides"}


def _list_tables():
    return common.bq_query("SELECT table_id, type, row_count, FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', "
                           "TIMESTAMP_MILLIS(last_modified_time)) AS last_modified "
                           "FROM `fall26-ails-termproj-nba.nba.__TABLES__` ORDER BY table_id")


def datasets_status():
    """Row counts and last-modified time for the tables the GUI may show (HIDDEN_TABLES are left out).

    BigQuery's metadata reports 0 rows for a VIEW (a view stores none), so for views the real count is queried."""
    out = []
    for r in _list_tables():
        name = r["table_id"]
        if name in HIDDEN_TABLES:
            continue
        count = r["row_count"]
        if str(r.get("type")) == "2" and re.fullmatch(r"[A-Za-z0-9_]+", name):   # 2 = VIEW
            try:
                count = common.bq_query(f"SELECT COUNT(*) AS n FROM nba.{name}")[0]["n"]
            except Exception:
                pass                                                              # keep the metadata value
        out.append({"table_id": name, "row_count": count, "last_modified": r["last_modified"]})
    return out


# Tables whose full contents are sensitive (people's emails, the questions they asked, free text). For these only the
# listed columns are ever selected, so the rest is never even read.
RESTRICTED_TABLES = {
    "run_log": ["run_ts", "agent", "step", "decision", "status"],
    "demo_changes": ["option", "hcp_id", "applied_at", "reverted"],
    "demo_overrides": ["key", "applied_at"],
}
PREVIEW_ROWS = 5
PREVIEW_CELL_MAX = 80


def table_preview(name):
    """Five sample rows of one table of the pipeline dataset, for the console.

    SAFETY: `name` must be one of the tables that really exist in the dataset (never interpolated otherwise), the query is
    one fixed shape (SELECT <columns> FROM nba.<table> LIMIT 5), sensitive tables expose only whitelisted columns, and long
    values are shortened. Raises ValueError for an unknown table.
    """
    name = str(name or "")
    known = {r["table_id"] for r in _list_tables() if r["table_id"] not in HIDDEN_TABLES}   # names only: no view counting
    if not re.fullmatch(r"[A-Za-z0-9_]+", name) or name in HIDDEN_TABLES or name not in known:
        raise ValueError("unknown table")
    cols = RESTRICTED_TABLES.get(name)
    select = ", ".join(cols) if cols else "*"
    rows = common.bq_query(f"SELECT {select} FROM nba.{name} LIMIT {PREVIEW_ROWS}")
    columns = list(rows[0].keys()) if rows else (cols or [])
    if cols:                                            # defence in depth: never return a column that is not whitelisted
        columns = [c for c in columns if c in cols]

    def cell(v):
        if v is None:
            return None
        t = v if isinstance(v, str) else json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
        return t if len(t) <= PREVIEW_CELL_MAX else t[:PREVIEW_CELL_MAX - 1] + "…"
    return {"table": name, "columns": columns, "rows": [[cell(r.get(c)) for c in columns] for r in rows],
            "restricted": bool(cols),
            "note": "Sensitive columns are hidden for this table." if cols else None}


def refresh_features():
    sql = (HERE / "02_build_features.sql").read_text()
    stmts = [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]
    for s in stmts:
        common.bq_query(s)
    return {"statements_run": len(stmts)}


# ---------------------------------------------------------------------------------------- Gate 1
def gate1(state):
    n, cut, latest = state["new_outcome_weeks"], state["model_cutoff"], state["latest_outcome_week"]
    ver = state["layer1_model_version"] or ""
    mv = re.match(r"v(\d+)-", ver)
    next_version = f"v{int(mv.group(1)) + 1 if mv else 1}-thru-{latest}"
    if cut is None:
        return {"decision": "RETRAIN", "next_version": next_version,
                "reason": "The current model version has no training cutoff, so it cannot be confirmed fresh; retraining."}
    if n >= MIN_NEW_WEEKS:
        return {"decision": "RETRAIN", "next_version": next_version,
                "reason": (f"{n} new outcome weeks arrived after the model's training cutoff ({cut}). "
                           f"The rule retrains at {MIN_NEW_WEEKS} or more, so the model will be retrained through {latest}.")}
    if n == 1:
        return {"decision": "HOLD", "next_version": None,
                "reason": (f"Only 1 new outcome week since the cutoff ({cut}). The rule needs {MIN_NEW_WEEKS} or more, "
                           f"so the model is kept as it is.")}
    return {"decision": "HOLD", "next_version": None,
            "reason": f"The model was trained through {cut} and the latest outcomes are for {latest}: nothing new, so no retrain."}


# ------------------------------------------------------------------------------------ Layer 1 steps
def drift_check(cutoff):
    """Train through the cutoff, then score the randomized outcomes that arrived AFTER it."""
    import numpy as np
    cols = ", ".join(["hcp_id", "week", "action", "nbrx_2w"] + base.CAT + base.NUM)
    df = common.bq_df(f"SELECT {cols} FROM nba.uplift_train WHERE is_randomized ORDER BY hcp_id, week").reset_index(drop=True)
    tr = df[df.week <= cutoff].reset_index(drop=True)
    new = df[df.week > cutoff].reset_index(drop=True)
    if len(new) < 500:
        return {"error": "fewer than 500 new randomized rows after the cutoff", "n_new_rows": int(len(new))}
    models, enc = base.fit_tlearner(tr)
    up = base.predict_uplift(models, enc, new)
    elig = up.copy()
    elig.columns = base.CHANNELS
    elig.loc[new.email_consent.values == 0, "email"] = -np.inf
    ch, best = elig.idxmax(axis=1), elig.max(axis=1)
    sel = (best.rank(ascending=False, method="first") / len(new) <= 0.30) & (best > 0)
    y, act = new.nbrx_2w.values, new.action.values
    real = (((act == ch.values) * y) - ((act == "none") * y)) / base.PROP
    g = np.column_stack([sel.values, real * sel.values, best.values * sel.values])
    keys, inv = np.unique(new.hcp_id.values, return_inverse=True)
    S = np.zeros((len(keys), 3))
    np.add.at(S, inv, g)
    rng = np.random.default_rng(base.SEED)

    def ratio(ix):
        s = S[ix].sum(axis=0)
        return s[1] / s[0], s[2] / s[0]
    (r, p) = ratio(np.arange(len(S)))
    ci = np.percentile([ratio(rng.integers(0, len(S), len(S)))[0] for _ in range(200)], [2.5, 97.5])
    return {"trained_through": cutoff, "n_new_rows": int(len(new)), "selected_contacts": int(sel.sum()),
            "predicted_uplift_per_contact": round(float(p), 3), "realized_uplift_per_contact": round(float(r), 3),
            "realized_ci95": [round(float(ci[0]), 3), round(float(ci[1]), 3)],
            "realized_over_predicted": round(float(r / p), 3) if p else None}


def retrain(through_week, version):
    cols = ", ".join(["hcp_id", "week", "action", "nbrx_2w"] + base.CAT + base.NUM)
    df = common.bq_df(f"SELECT {cols} FROM nba.uplift_train WHERE is_randomized AND week <= '{through_week}' "
                      f"ORDER BY hcp_id, week").reset_index(drop=True)
    rec = base.score_and_write(df, version, write_files=False)
    return {"model_version": version, "trained_through": through_week, "train_rows": int(len(df)),
            "layer1_mix": rec.recommended_channel.value_counts().to_dict()}


# ---------------------------------------------------------------------------------------- Gate 2
def gate2():
    """Dry run of Layer 2 and a diff against what is published. Returns (out_df, meta, decision_dict)."""
    out, meta = layer2.compute()
    d = layer2.diff(layer2.read_published(), out)
    if d["publish_needed"]:
        parts = []
        if d["layer1_changed"]:
            parts.append(f"{d['layer1_changed']} because Layer 1 produced a new model or scores")
        if d["channel_or_contact_changed"]:
            parts.append(f"{d['channel_or_contact_changed']} with a different channel or contact decision")
        if d["message_changed"]:
            parts.append(f"{d['message_changed']} with a different message")
        if d["newly_blocked_by_compliance"]:
            parts.append(f"{d['newly_blocked_by_compliance']} newly blocked by the compliance filter")
        if d.get("score_or_text_only_changed") and not d["layer1_changed"]:
            parts.append(f"{d['score_or_text_only_changed']} with only a rank or score wording update "
                         f"(ranks shift when one HCP's channel changes)")
        reason = (f"Layer 2 was computed in memory and differs from the published table for {d['changed_hcps']} of "
                  f"{d['total']} HCPs ({'; '.join(parts) or 'text or score updates only'}). It will be republished.")
        decision = "RUN"
    else:
        reason = (f"Layer 2 was computed in memory and matches the published table exactly (0 of {d['total']} HCPs differ), "
                  f"so nothing is republished.")
        decision = "SKIP"
    return out, meta, {**d, "decision": decision, "reason": reason}


def preview():
    """What the next run WOULD do. Refreshes features (idempotent) and dry-runs both gates; publishes nothing."""
    refresh_features()
    st = get_state()
    g1 = gate1(st)
    out, meta, g2 = gate2()
    if g1["decision"] == "RETRAIN":
        g2 = {**g2, "decision": "RUN", "reason": "Layer 1 will be retrained first, so its scores change and Layer 2 will run "
              "on the new model. (The exact Layer 2 changes are known after Layer 1 runs.)"}
    return {"state": st, "gate1": g1, "gate2": {k: v for k, v in g2.items()}}


# -------------------------------------------------------------------------------------------- verify
def verify():
    pub = snapshot()
    l1 = get_state()["layer1_model_version"]
    ref = layer2.compliance.fetch_public_reference()
    checks = [
        {"name": "Layer 2 is built on the current Layer 1 model", "ok": pub["model_version"] == l1,
         "detail": f"Layer 1 {l1}, Layer 2 {pub['model_version']}"},
        {"name": "No messages failed the compliance filter", "ok": pub["compliance_failures"] == 0,
         "detail": (f"{pub['compliance_failures']} failed; {pub['withheld']} contacts withheld (fail closed)"
                    if pub["compliance_failures"] else "0 failures")},
        {"name": "Contacts within the 30% budget", "ok": pub["contacts"] <= 0.30 * pub["hcps"] + 1,
         "detail": f"{pub['contacts']} of {pub['hcps']}"},
        {"name": "Approved claim cross-checked against DailyMed", "ok": "match" in (ref.get("claim_source") or "").lower()
         and "not confirmed" not in (ref.get("claim_source") or "").lower(),
         "detail": ref.get("claim_source", "")[:140]},
    ]
    return {"status": "PASS" if all(c["ok"] for c in checks) else "ATTENTION", "checks": checks, "published": pub}


# ---------------------------------------------------------------------------------------- narration
NARRATOR = ("You explain steps of a pharma analytics workflow to a business audience. Write 1 to 2 plain sentences. "
            "Use ONLY the numbers and facts provided; never invent numbers. No headings, no markdown, no jargon "
            "like 'T-learner'.")


def _llm(prompt, max_tokens=1500):
    r = common.gemini([{"role": "user", "parts": [{"text": prompt}]}], system=NARRATOR, temperature=0,
                      max_tokens=max_tokens)
    cand = r["candidates"][0]
    if cand.get("finishReason") == "MAX_TOKENS":      # truncated text is worse than the deterministic fallback
        return None
    parts = cand.get("content", {}).get("parts", [])
    return " ".join(p.get("text", "") for p in parts if p.get("text")).strip() or None


def narrate(step_id, reason, facts):
    try:
        return _llm(f"Step: {step_id}\nDecision/reason (already decided by code): {reason}\n"
                    f"Facts: {json.dumps(facts, default=str)[:1500]}\nExplain this step.")
    except Exception:
        return None


def final_report(run):
    summary = [{"step": s["id"], "status": s["status"], "reason": s["reason"]} for s in run.steps]
    facts = {"before": run.before, "after": run.after, "steps": summary, "verification": run.verify}
    try:
        txt = _llm("Write a short report (max 110 words) of this workflow run as 3 to 5 bullet lines starting with '- '. "
                   "Cover: what was found, what was decided and why, before versus after, and any concern the verification "
                   "raised (state plainly if the run needs attention). Use only these facts.\n"
                   f"FACTS: {json.dumps(facts, default=str)[:3500]}", max_tokens=3000)
        if txt:
            return txt
    except Exception:
        pass
    v = run.verify or {}
    return "\n".join(["- " + s["reason"] for s in run.steps if s["status"] != "skipped"][:5] +
                     [f"- Verification: {v.get('status', 'n/a')}"])


# ------------------------------------------------------------------------------------------ the run
class Run:
    def __init__(self, mode="auto", narrate_on=True, echo=False):
        self.id = uuid.uuid4().hex[:12]
        self.mode, self.narrate_on, self.echo = mode, narrate_on, echo
        self.status = "running"          # running | waiting | done | attention | failed | aborted
        self.steps, self.waiting = [], None
        self.before = self.after = self.verify = self.report = self.error = None
        self.started, self.finished = time.time(), None
        self._cv, self._action = threading.Condition(), None

    # -- step bookkeeping ------------------------------------------------------------------
    def add(self, sid, title):
        s = {"id": sid, "title": title, "status": "running", "reason": "", "facts": {}, "narration": None,
             "t0": time.time(), "seconds": None}
        self.steps.append(s)
        return s

    def done(self, s, status, reason, facts=None):
        s.update(status=status, reason=reason, facts=facts or {}, seconds=round(time.time() - s["t0"], 1))
        if self.narrate_on:
            s["narration"] = narrate(s["id"], reason, facts or {})
        try:
            common.log_run(self.id, AGENT, s["id"], status, rationale=reason, inputs=self.mode,
                           outputs=json.dumps({"facts": facts, "narration": s["narration"]}, default=str))
        except Exception:
            pass
        if self.echo:
            print(f"[{s['id']}] {status.upper()}: {reason}" + (f"\n    {s['narration']}" if s["narration"] else ""), flush=True)

    # -- pausing (stepper mode) --------------------------------------------------------------
    def wait(self, key, prompt, options):
        with self._cv:
            self.waiting, self.status, self._action = {"key": key, "prompt": prompt, "options": options}, "waiting", None
            deadline = time.time() + WAIT_TIMEOUT_S
            while self._action is None and time.time() < deadline:
                self._cv.wait(timeout=2)
            action = self._action or "abort"
            self.waiting, self.status = None, "running"
            return action

    def control(self, action):
        with self._cv:
            if not self.waiting or action not in self.waiting["options"]:
                return False
            self._action = action
            self._cv.notify_all()
            return True

    def view(self):
        return {"id": self.id, "mode": self.mode, "status": self.status, "waiting": self.waiting, "error": self.error,
                "steps": [{k: v for k, v in s.items() if k != "t0"} for s in self.steps],
                "before": self.before, "after": self.after, "verify": self.verify, "report": self.report,
                "elapsed": round((self.finished or time.time()) - self.started, 1)}


def _execute(run):
    stepper = run.mode == "stepper"
    run.before = snapshot()
    common.log_run(run.id, AGENT, "start", run.mode, rationale="begin workflow", status="started")

    s = run.add("refresh", "Refresh features from the raw tables")
    run.done(s, "done", "Rebuilt the feature tables from the raw data so any changed or newly added source rows are used.",
             refresh_features())

    s = run.add("freshness", "Freshness check and Layer 1 gate")
    state = get_state()
    g1 = gate1(state)
    run.done(s, "done", g1["reason"], {**state, "decision": g1["decision"]})
    if stepper and run.wait("gate1", f"Gate 1 decided: {g1['decision']}. {g1['reason']} "
                      + ("Continue to retrain the model?" if g1["decision"] == "RETRAIN" else "Continue to the Layer 2 check?"),
                      ["continue", "abort"]) == "abort":
        run.status = "aborted"
        return

    if g1["decision"] == "RETRAIN":
        s = run.add("layer1_drift", "Layer 1: test the current model on the unseen weeks")
        d = drift_check(state["model_cutoff"]) if state["model_cutoff"] else {"error": "no cutoff"}
        if "realized_uplift_per_contact" in d:
            rsn = (f"The current model predicted +{d['predicted_uplift_per_contact']} NBRx per contact on the "
                   f"{d['n_new_rows']:,} unseen rows; the realized lift was {d['realized_uplift_per_contact']:+} "
                   f"(95% CI {d['realized_ci95'][0]} to {d['realized_ci95'][1]}).")
        else:
            rsn = f"The drift check could not run: {d.get('error')}."
        run.done(s, "done", rsn, d)
        s = run.add("layer1_retrain", "Layer 1: retrain and rescore")
        r = retrain(state["latest_outcome_week"], g1["next_version"])
        run.done(s, "done", f"Retrained on all randomized outcomes through {r['trained_through']} as {r['model_version']} "
                            f"({r['train_rows']:,} rows) and wrote nba.recommendations.", r)
    else:
        s = run.add("layer1", "Layer 1 model")
        run.done(s, "skipped", "Not retrained. " + g1["reason"], {"decision": "HOLD"})

    while True:
        s = run.add("layer2_gate", "Layer 2 gate: would anything change?")
        out, meta, g2 = gate2()
        run.done(s, "done", g2["reason"], {k: v for k, v in g2.items() if k != "reason"})
        if not stepper:
            break
        prompt = (f"Layer 2 will {'RUN' if g2['decision'] == 'RUN' else 'be SKIPPED'}. "
                  + ("Continue to publish it." if g2["decision"] == "RUN" else "Nothing would change. Continue to skip it."))
        a = run.wait("gate2", prompt, ["continue", "recheck", "abort"])
        if a == "abort":
            run.status = "aborted"
            return
        if a == "recheck":
            s["status"] = "superseded"
            refresh_features()
            continue
        break

    if g2["decision"] == "RUN":
        s = run.add("layer2", "Layer 2: publish")
        layer2.publish(out)
        summ = layer2.summarize(out, meta)
        run.done(s, "done", f"Published nba.recommendations_final: {summ['contacts']} contacts, {summ['substituted']} channel "
                            f"substitutions, {summ['compliance_failures']} compliance failures.", summ)
    else:
        s = run.add("layer2", "Layer 2: publish")
        run.done(s, "skipped", "Not republished: the dry run found no differences.", {})

    s = run.add("verify", "Verify before reporting")
    run.verify = verify()
    bad = [c for c in run.verify["checks"] if not c["ok"]]
    run.done(s, "done" if not bad else "attention",
             "All checks passed." if not bad else "Needs attention: " + "; ".join(f"{c['name']} ({c['detail']})" for c in bad),
             run.verify)
    run.after = snapshot()
    s = run.add("report", "Report")
    run.report = final_report(run)
    run.done(s, "done", "Run complete.", {})
    run.status = "attention" if bad else "done"


def execute(run):
    """Run the workflow; never raises (errors are stored on the run)."""
    try:
        _execute(run)
    except Exception as e:  # noqa: BLE001
        run.status, run.error = "failed", str(e)[:500]
        try:
            common.log_run(run.id, AGENT, "error", "failed", rationale=run.error, status="error")
        except Exception:
            pass
    finally:
        run.finished = time.time()
        try:
            common.log_run(run.id, AGENT, "finish", run.status, rationale=(run.report or run.error or "")[:3000],
                           status=run.status)
        except Exception:
            pass
    return run


def run_auto(narrate_on=True, echo=True):
    run = Run("auto", narrate_on, echo)
    execute(run)
    if echo:
        print("\n=== REPORT ===\n" + (run.report or run.error or "") + f"\n\nstatus={run.status}  run_id={run.id}")
    return run


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        r = run_auto(narrate_on="--no-narrate" not in sys.argv)
        sys.exit(0 if r.status in ("done", "attention") else 1)
    print(__doc__)
