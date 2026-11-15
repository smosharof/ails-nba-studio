#!/usr/bin/env python3
"""Demo data controls. Small, labelled, fully reversible changes to the SOURCE data so each part of
the workflow can be shown on demand:

  A  Frequency cap        add ONE contact-history row for an HCP (3 calls in the last 4 weeks)
                          -> Layer 2 blocks that HCP's call and substitutes another channel
  B  Consent withdrawal   set ONE HCP's email consent to 0
                          -> Layer 2 blocks email for that HCP
  C  Compliance guard     put banned wording (and a claim that is not the approved one) into one channel's
                          message copy -> the compliance filter fails closed and withholds those contacts
  E  Rewind the model     retrain Layer 1 only through an earlier week, as if the latest outcomes had
                          not arrived yet -> Gate 1 will say RETRAIN

Every change is recorded in nba.demo_changes (with the original value) or nba.demo_overrides, and
reset() restores the production state exactly: injected rows removed, consent restored, overrides
cleared, the production model re-trained if it was rewound, and Layer 2 republished.
None of this touches the original course files.
"""
import json
import re
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import layer2  # noqa: E402
import workflow  # noqa: E402

UNSAFE_CLAIM = "as the best option to improve glycemic control in adults with type 2 diabetes mellitus"
HCP_RE = re.compile(r"H\d{4,8}$")


_TABLES_READY = False
_TABLES_LOCK = threading.Lock()


def ensure_tables():
    """Create the two bookkeeping tables once per process.

    BigQuery counts every CREATE TABLE (even IF NOT EXISTS) as a table update and allows only about 5 per 10 seconds per
    table, so running this on every page refresh made the status card fail with 'exceeded quota for table update
    operations'. Nothing here ever drops those tables, so checking once is enough."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _TABLES_LOCK:
        if _TABLES_READY:
            return
        common.bq_query("CREATE TABLE IF NOT EXISTS nba.demo_changes (change_id STRING, option STRING, hcp_id STRING, "
                        "detail STRING, applied_at TIMESTAMP, reverted BOOL)")
        common.bq_query("CREATE TABLE IF NOT EXISTS nba.demo_overrides (key STRING, value STRING, applied_at TIMESTAMP)")
        _TABLES_READY = True


def _check_hcp(hcp_id):
    if not HCP_RE.match(str(hcp_id or "")):
        raise ValueError("hcp_id must look like H100123")
    if not common.bq_query(f"SELECT 1 AS x FROM nba.raw_hcp_master WHERE hcp_id = '{hcp_id}'"):
        raise ValueError(f"{hcp_id} is not in the HCP master table")
    return hcp_id


def _log_change(option, hcp_id, detail):
    cid = uuid.uuid4().hex[:10]
    d = json.dumps(detail).replace("\\", "\\\\").replace("'", "\\'")
    common.bq_query("INSERT INTO nba.demo_changes (change_id, option, hcp_id, detail, applied_at, reverted) "
                    f"VALUES ('{cid}', '{option}', '{hcp_id}', '{d}', CURRENT_TIMESTAMP(), FALSE)")
    return cid


def active_changes():
    ensure_tables()
    ch = common.bq_query("SELECT change_id, option, hcp_id, detail, CAST(applied_at AS STRING) AS applied_at "
                         "FROM nba.demo_changes WHERE NOT reverted ORDER BY applied_at")
    ov = common.bq_query("SELECT key, value FROM nba.demo_overrides")
    return {"changes": ch, "overrides": ov}


# ------------------------------------------------------------------------------------------ options
def options():
    """What can be injected, with candidates taken from the current published recommendations."""
    ensure_tables()
    cands = lambda ch: common.bq_query(  # noqa: E731
        "SELECT hcp_id, priority_rank, segment FROM nba.recommendations_final "
        f"WHERE contact_flag AND final_channel = '{ch}' ORDER BY priority_rank LIMIT 6")
    mix = {r["final_channel"]: r["n"] for r in common.bq_query(
        "SELECT final_channel, COUNT(*) AS n FROM nba.recommendations_final WHERE contact_flag GROUP BY 1")}
    weeks = [r["w"] for r in common.bq_query(
        f"SELECT DISTINCT CAST(week AS STRING) AS w FROM nba.raw_touch_log WHERE nbrx_2w IS NOT NULL "
        f"AND week <= DATE '{workflow.ORIGINAL_MAX_WEEK}' ORDER BY w")]
    return {
        "A": {"title": "A. Frequency cap", "layer": "Layer 2",
              "what": "Add one contact-history row: the chosen HCP has already received 3 calls in the last 4 weeks.",
              "expect": "Layer 1: HOLD. Layer 2 gate: RUN. That HCP's call is blocked by the frequency cap and Layer 2 "
                        "switches them to the next-best allowed channel (or no contact).",
              "candidates": cands("call")},
        "B": {"title": "B. Consent withdrawal", "layer": "Layer 2",
              "what": "Set one HCP's email consent to 0 (they opted out of email).",
              "expect": "Layer 1: HOLD. Layer 2 gate: RUN. That HCP's email is blocked and Layer 2 switches the channel.",
              "candidates": cands("email")},
        "C": {"title": "C. Compliance guard", "layer": "Layer 2 + verify",
              "what": "Change one channel's message copy to unapproved wording that contains the banned word 'best'.",
              "expect": "Layer 1: HOLD. Layer 2 gate: RUN. Every message on that channel fails the compliance filter, "
                        "those contacts are withheld (fail closed), and Verify reports NEEDS ATTENTION instead of success.",
              "channels": [{"channel": c, "contacts": mix.get(c, 0)} for c in ("digital", "call", "email")]},
        "E": {"title": "E. Rewind the model", "layer": "Layer 1 + Layer 2",
              "what": "Retrain Layer 1 only through an earlier week, as if the latest outcomes had not arrived yet.",
              "expect": "Gate 1: RETRAIN when 2 or more weeks are unseen (HOLD when only 1). After a retrain, Layer 2 runs "
                        "because the Layer 1 scores changed.",
              "choices": [{"weeks_back": wb, "model_cutoff": weeks[-1 - wb], "new_outcome_weeks": wb,
                           "gate1": "RETRAIN" if wb >= workflow.MIN_NEW_WEEKS else "HOLD"} for wb in (1, 2, 4, 8)]},
    }


# ---------------------------------------------------------------------------------------- injections
def inject_frequency_cap(hcp_id, calls=3):
    ensure_tables()
    _check_hcp(hcp_id)
    calls = int(calls)
    if not 2 <= calls <= 4:
        raise ValueError("calls must be 2 to 4")
    if common.bq_query(f"SELECT 1 AS x FROM nba.demo_changes WHERE NOT reverted AND option='A' AND hcp_id='{hcp_id}'"):
        raise ValueError(f"{hcp_id} already has an active frequency-cap change")
    wk = common.bq_query("SELECT CAST(DATE_ADD(MAX(week), INTERVAL 7 DAY) AS STRING) AS w FROM nba.raw_touch_log")[0]["w"]
    common.bq_query(
        "INSERT INTO nba.raw_touch_log (hcp_id, week, arm, action, propensity, calls_4w, emails_4w, digital_4w, "
        "weeks_since_call, last_action, trailing_nbrx_wk, engaged, complaint, nbrx_2w) "
        f"SELECT hcp_id, DATE '{wk}', 'legacy', 'call', propensity, {calls}, emails_4w, digital_4w, 0, 'call', "
        "trailing_nbrx_wk, 0, 0, CAST(NULL AS INT64) FROM nba.raw_touch_log "
        f"WHERE hcp_id = '{hcp_id}' QUALIFY ROW_NUMBER() OVER (ORDER BY week DESC) = 1")
    cid = _log_change("A", hcp_id, {"calls_4w": calls, "new_row_week": wk})
    return {"option": "A", "change_id": cid, "hcp_id": hcp_id,
            "summary": f"Added a contact-history row for {hcp_id} (week {wk}): {calls} calls in the last 4 weeks. "
                       f"The outcome is not known yet, so this is not counted as an outcome week."}


def inject_consent_withdrawal(hcp_id):
    ensure_tables()
    _check_hcp(hcp_id)
    before = common.bq_query(f"SELECT email_consent FROM nba.raw_hcp_segments WHERE hcp_id = '{hcp_id}'")
    if not before:
        raise ValueError(f"{hcp_id} has no segment row")
    if int(before[0]["email_consent"]) == 0:
        raise ValueError(f"{hcp_id} already has no email consent")
    common.bq_query(f"UPDATE nba.raw_hcp_segments SET email_consent = 0 WHERE hcp_id = '{hcp_id}'")
    cid = _log_change("B", hcp_id, {"email_consent_before": int(before[0]["email_consent"])})
    return {"option": "B", "change_id": cid, "hcp_id": hcp_id,
            "summary": f"Set email consent to 0 for {hcp_id} (was {int(before[0]['email_consent'])})."}


def inject_unsafe_claim(channel):
    ensure_tables()
    if channel not in ("call", "email", "digital"):
        raise ValueError("channel must be call, email or digital")
    common.bq_query("DELETE FROM nba.demo_overrides WHERE key = 'claim_override'")
    val = json.dumps({"channel": channel, "claim": UNSAFE_CLAIM}).replace("\\", "\\\\").replace("'", "\\'")
    common.bq_query(f"INSERT INTO nba.demo_overrides (key, value, applied_at) VALUES ('claim_override', '{val}', CURRENT_TIMESTAMP())")
    _log_change("C", "-", {"channel": channel, "claim": UNSAFE_CLAIM})
    return {"option": "C", "channel": channel,
            "summary": f"The {channel} message copy now reads \"...{UNSAFE_CLAIM}...\" (contains the banned word 'best' "
                       f"and is not the approved claim)."}


def rewind_model(weeks_back):
    """Retrain Layer 1 through an earlier week and republish Layer 2, so the published state is consistently 'stale'."""
    ensure_tables()
    weeks_back = int(weeks_back)
    if weeks_back not in (1, 2, 4, 8):
        raise ValueError("weeks_back must be 1, 2, 4 or 8")
    weeks = [r["w"] for r in common.bq_query(
        f"SELECT DISTINCT CAST(week AS STRING) AS w FROM nba.raw_touch_log WHERE nbrx_2w IS NOT NULL "
        f"AND week <= DATE '{workflow.ORIGINAL_MAX_WEEK}' ORDER BY w")]
    cutoff = weeks[-1 - weeks_back]
    version = f"v0-thru-{cutoff}"
    r = workflow.retrain(cutoff, version)
    out, meta = layer2.compute()
    layer2.publish(out)
    _log_change("E", "-", {"weeks_back": weeks_back, "model_cutoff": cutoff, "model_version": version})
    return {"option": "E", "model_version": version, "model_cutoff": cutoff, "new_outcome_weeks": weeks_back,
            "summary": f"Layer 1 was retrained only through {cutoff} ({version}), so {weeks_back} outcome week(s) are unseen. "
                       f"Layer 2 was republished on it."}


# -------------------------------------------------------------------------------------------- reset
def reset():
    """Restore the production state exactly."""
    ensure_tables()
    steps = []
    n = common.bq_query(f"SELECT COUNT(*) AS n FROM nba.raw_touch_log WHERE week > DATE '{workflow.ORIGINAL_MAX_WEEK}'")[0]["n"]
    common.bq_query(f"DELETE FROM nba.raw_touch_log WHERE week > DATE '{workflow.ORIGINAL_MAX_WEEK}'")
    steps.append(f"removed {n} injected contact-history row(s)")
    for ch in common.bq_query("SELECT hcp_id, detail FROM nba.demo_changes WHERE NOT reverted AND option = 'B'"):
        before = int(json.loads(ch["detail"])["email_consent_before"])
        common.bq_query(f"UPDATE nba.raw_hcp_segments SET email_consent = {before} WHERE hcp_id = '{ch['hcp_id']}'")
        steps.append(f"restored email consent for {ch['hcp_id']}")
    common.bq_query("DELETE FROM nba.demo_overrides WHERE TRUE")
    common.bq_query("UPDATE nba.demo_changes SET reverted = TRUE WHERE NOT reverted")
    workflow.refresh_features()
    ver = workflow.get_state()["layer1_model_version"]
    if ver != workflow.BASELINE_VERSION:
        workflow.retrain(workflow.ORIGINAL_MAX_WEEK, workflow.BASELINE_VERSION)
        steps.append(f"re-trained the production model ({workflow.BASELINE_VERSION}); it was {ver}")
    out, meta = layer2.compute()
    layer2.publish(out)
    steps.append("republished Layer 2")
    return {"summary": "Reset to production: " + "; ".join(steps) + ".", "steps": steps}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "reset":
        print(reset())
    elif cmd == "options":
        print(json.dumps(options(), indent=1, default=str)[:3000])
    else:
        print(__doc__)
