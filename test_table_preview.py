#!/usr/bin/env python3
"""Attack tests for the console's table previews (workflow.table_preview). Offline: BigQuery is faked.

Run:  python3 -I test_table_preview.py        (exits non-zero if any check fails)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
FAILS, PASSES = [], 0


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print("  FAIL:", name, detail)


def main():
    import workflow as w
    sqls = []
    state = {"rows": [{"a": 1}]}
    w._list_tables = lambda: [{"table_id": t, "type": 2 if t.startswith("v_") else 1, "row_count": 0, "last_modified": "x"} for t in ("features", "run_log", "demo_changes", "demo_overrides", "recommendations_final", "v_recommendations")]
    w.common.bq_query = lambda sql: sqls.append(sql) or state["rows"]

    # ---- the name must be one of the real tables; nothing else is ever put in SQL
    bad = ["nope", "features; DROP TABLE x", "features`", "nba.run_log", "../x", "", None, "FEATURES", "features ", " features",
           "features\n", "features--", "run_log UNION SELECT 1", "`fall26-ails-termproj-nba.nba.run_log`", "*", "features,run_log", "x" * 500, "ｆeatures"]
    for n in bad:
        try:
            w.table_preview(n)
            check(f"rejected name {n!r}", False, "it was accepted")
        except ValueError:
            check(f"rejected name {n!r}", True)
    check("no query was run for any rejected name", sqls == [], sqls)

    check("the list shown to the console leaves out run_log, demo_changes, demo_overrides", [r["table_id"] for r in w.datasets_status()] == ["features", "recommendations_final", "v_recommendations"], w.datasets_status())
    del sqls[:]                                   # the line above counted the view; start the next checks clean
    for n in ("run_log", "demo_changes", "demo_overrides"):
        try:
            w.table_preview(n); check(f"hidden table {n} cannot be previewed", False, "it was accepted")
        except ValueError:
            check(f"hidden table {n} cannot be previewed", True)
    check("no query ran for the hidden tables", sqls == [], sqls)

    # ---- views report 0 rows in BigQuery metadata, so the real count is queried (and only for views)
    keep = w.common.bq_query
    w.common.bq_query = lambda sql: sqls.append(sql) or [{"n": 6000}]
    rows = {r["table_id"]: r for r in w.datasets_status()}
    check("a view shows its real row count, not the metadata 0", rows["v_recommendations"]["row_count"] == 6000, rows["v_recommendations"])
    check("only views are counted (tables keep their metadata count)", rows["features"]["row_count"] == 0 and sqls == ["SELECT COUNT(*) AS n FROM nba.v_recommendations"], sqls)
    check("the list carries only table_id, row_count, last_modified", set(rows["features"]) == {"table_id", "row_count", "last_modified"})
    def boom(sql): raise RuntimeError("bq down")
    w.common.bq_query = boom
    check("if counting a view fails, the page still gets the metadata value", {r["table_id"]: r for r in w.datasets_status()}["v_recommendations"]["row_count"] == 0)
    w.common.bq_query = keep
    del sqls[:]

    # ---- an ordinary table: one fixed query shape
    state["rows"] = [{"hcp_id": "H1", "segment": "Movers"}]
    r = w.table_preview("features")
    check("ordinary table: fixed query shape", sqls[-1] == "SELECT * FROM nba.features LIMIT 5", sqls[-1])
    check("ordinary table: columns and rows returned", r["columns"] == ["hcp_id", "segment"] and r["rows"] == [["H1", "Movers"]] and not r["restricted"])
    r = w.table_preview("v_recommendations")
    check("a view can be previewed too", sqls[-1] == "SELECT * FROM nba.v_recommendations LIMIT 5")

    # ---- defence in depth: if a sensitive table were ever un-hidden, only whitelisted columns are even selected
    w.HIDDEN_TABLES = set()
    for t, hidden, shown in [("run_log", ["rationale", "inputs", "outputs", "run_id"], ["run_ts", "agent", "step", "decision", "status"]),
                             ("demo_changes", ["detail", "change_id"], ["option", "hcp_id", "applied_at", "reverted"]),
                             ("demo_overrides", ["value"], ["key", "applied_at"])]:
        state["rows"] = [{**{c: "x" for c in shown}, **{c: "SECRET user@example.com asked something" for c in hidden}}]
        r = w.table_preview(t)
        sql = sqls[-1]
        check(f"{t}: only whitelisted columns are selected", "SELECT *" not in sql and all(c in sql for c in shown) and not any(c in sql for c in hidden), sql)
        check(f"{t}: hidden columns never come back, even if the database returned them", set(r["columns"]) == set(shown) and "SECRET" not in str(r["rows"]), r["columns"])
        check(f"{t}: flagged as restricted with a note", r["restricted"] and "hidden" in (r["note"] or ""))

    # ---- values: shortened, nulls kept, nested values flattened
    state["rows"] = [{"msg": "m" * 500, "n": None, "d": {"a": [1, 2]}, "i": 7, "b": True}]
    r = w.table_preview("features")
    row = dict(zip(r["columns"], r["rows"][0]))
    check("long values are shortened to 80 characters with an ellipsis", len(row["msg"]) == 80 and row["msg"].endswith("…"), len(row["msg"]))
    check("nulls stay null; numbers and booleans become text", row["n"] is None and row["i"] == "7" and row["b"] == "True")
    check("nested values are flattened to JSON text", row["d"] == '{"a": [1, 2]}', row["d"])

    # ---- empty tables
    state["rows"] = []
    r = w.table_preview("features"); check("an empty table returns no rows and no error", r["rows"] == [] and r["columns"] == [])
    r = w.table_preview("run_log"); check("an empty restricted table still shows only the whitelisted headers", r["columns"] == ["run_ts", "agent", "step", "decision", "status"])

    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
