#!/usr/bin/env python3
"""Attack tests for the text-to-SQL tool (sqltool.py).

Offline (default): the text validator and the dry-run check, with benign and hostile SQL.
Live  (--live)   : against the real restricted service account and BigQuery. Proves that even if the text checks were
                   bypassed, the restricted identity cannot read anything outside the two curated views.

Run:  python3 -I test_sqltool.py            (offline)
      python3 -I test_sqltool.py --live     (also hits BigQuery; needs the setup script to have been run)
"""
import os
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
    import sqltool as t
    P, D = t.PROJECT, t.CHAT_DATASET

    # ---------------------------------------------------------------- benign queries must pass the text layer
    good = {
        "group by": "SELECT segment, COUNT(*) AS n FROM nba_chat.recommendations GROUP BY segment ORDER BY n DESC",
        "qualified names": f"SELECT COUNT(*) FROM `{P}.{D}.recommendations` WHERE contact_flag",
        "value 'call' is not the keyword CALL": "SELECT COUNT(*) FROM nba_chat.recommendations WHERE final_channel = 'call'",
        "value 'set' in LIKE is a string": "SELECT hcp_id FROM nba_chat.recommendations WHERE blocked_reason LIKE '%set%' LIMIT 5",
        "join": "SELECT r.segment, AVG(p.years_in_practice) FROM nba_chat.recommendations r JOIN nba_chat.hcp_profile p USING (hcp_id) GROUP BY r.segment",
        "with clause": "WITH x AS (SELECT segment FROM nba_chat.recommendations) SELECT segment, COUNT(*) FROM x GROUP BY segment",
        "trailing semicolon is cleaned": "SELECT 1 FROM nba_chat.recommendations;",
        "code fence is cleaned": "```sql\nSELECT COUNT(*) FROM nba_chat.recommendations\n```",
        "safe divide / consent": "SELECT SAFE_DIVIDE(COUNTIF(email_consent=1), COUNT(*)) FROM nba_chat.hcp_profile",
    }
    for n, q in good.items():
        ok, r = t.validate(q)
        check(f"allowed: {n}", ok, r)

    # ---------------------------------------------------------------- hostile queries must be rejected
    bad = {
        "DROP TABLE": "DROP TABLE nba_chat.recommendations",
        "DELETE": "DELETE FROM nba_chat.recommendations WHERE TRUE",
        "INSERT": "INSERT INTO nba_chat.recommendations (hcp_id) VALUES ('x')",
        "UPDATE": "UPDATE nba_chat.recommendations SET final_channel='call' WHERE TRUE",
        "MERGE": "MERGE nba_chat.recommendations T USING nba_chat.hcp_profile S ON TRUE WHEN MATCHED THEN DELETE",
        "CREATE TABLE AS SELECT": "CREATE TABLE nba_chat.x AS SELECT * FROM nba_chat.recommendations",
        "second statement": "SELECT 1 FROM nba_chat.recommendations; DROP TABLE nba_chat.recommendations",
        "second statement without space": "SELECT 1 FROM nba_chat.recommendations;DROP TABLE x",
        "line comment hides the rest": "SELECT 1 FROM nba_chat.recommendations -- WHERE x",
        "block comment": "SELECT /* hi */ 1 FROM nba_chat.recommendations",
        "hash comment": "SELECT 1 FROM nba_chat.recommendations # x",
        "production dataset (2-part)": "SELECT * FROM nba.run_log",
        "production dataset (3-part)": f"SELECT * FROM `{P}.nba.run_log`",
        "audit log by full name": f"SELECT rationale FROM {P}.nba.run_log",
        "other project": "SELECT * FROM `other-project.ds.t`",
        "INFORMATION_SCHEMA": f"SELECT * FROM {D}.INFORMATION_SCHEMA.TABLES",
        "SESSION_USER": "SELECT SESSION_USER()",
        "EXTERNAL_QUERY": "SELECT * FROM EXTERNAL_QUERY('x','select 1')",
        "CALL": "CALL nba_chat.p()",
        "EXPORT DATA": "EXPORT DATA OPTIONS(uri='gs://x/*') AS SELECT * FROM nba_chat.recommendations",
        "DECLARE": "DECLARE x INT64 DEFAULT 1",
        "SET": "SET @@dataset_id = 'nba'",
        "mixed case DrOp": "dRoP table nba_chat.recommendations",
        "leading blank lines": "\n\n  DROP TABLE x",
        "parenthesised start": "(SELECT 1 FROM nba_chat.recommendations)",
        "time travel": "SELECT * FROM nba_chat.recommendations FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-01'",
        "ML function": "SELECT * FROM ML.PREDICT(MODEL m, TABLE nba_chat.recommendations)",
        "zero-width character in SELECT": "SEL​ECT 1 FROM nba_chat.recommendations",
        "CANNOT_ANSWER marker": "CANNOT_ANSWER",
        "empty": "   ",
        "too long": "SELECT " + "1," * 3000 + "1 FROM nba_chat.recommendations",
    }
    for n, q in bad.items():
        ok, r = t.validate(q)
        check(f"rejected: {n}", not ok, r)

    # ---------------------------------------------------------------- BigQuery's dry-run report is checked too
    def view(x, ds=D, proj=P):
        return {"projectId": proj, "datasetId": ds, "tableId": x}

    def job(stmt="SELECT", views=("recommendations",), tables=(("nba", "recommendations_final"),), bytes_=1000):
        return {"statistics": {"totalBytesProcessed": str(bytes_), "query": {"statementType": stmt,
                "referencedLogicalViews": [view(v) for v in views],
                "referencedTables": [{"projectId": P, "datasetId": d, "tableId": x} for d, x in tables]}}}
    check("dry-run: normal select accepted", t.check_dry_run(job())[0])
    check("dry-run: join of both views accepted", t.check_dry_run(job(views=("recommendations", "hcp_profile"),
          tables=(("nba", "recommendations_final"), ("nba", "raw_hcp_master"), ("nba", "raw_hcp_segments"))))[0])
    check("dry-run: non-SELECT statement rejected", not t.check_dry_run(job(stmt="DELETE"))[0])
    check("dry-run: unknown view rejected", not t.check_dry_run(job(views=("secret",)))[0])
    check("dry-run: a view in another dataset rejected", not t.check_dry_run({"statistics": {"query": {"statementType": "SELECT",
          "referencedLogicalViews": [view("recommendations", ds="nba")], "referencedTables": []}}})[0])
    check("dry-run: a view in another project rejected", not t.check_dry_run({"statistics": {"query": {"statementType": "SELECT",
          "referencedLogicalViews": [view("recommendations", proj="evil")], "referencedTables": []}}})[0])
    check("dry-run: allowed view PLUS the audit log table rejected", not t.check_dry_run(job(tables=(("nba", "recommendations_final"), ("nba", "run_log"))))[0])
    check("dry-run: a production table read directly (no view) rejected", not t.check_dry_run(job(views=()))[0])
    check("dry-run: no views and no tables (SELECT 1) rejected", not t.check_dry_run(job(views=(), tables=()))[0])
    check("dry-run: too many bytes rejected", not t.check_dry_run(job(bytes_=10**9))[0])
    check("dry-run: missing statistics rejected", not t.check_dry_run({})[0])

    # ---------------------------------------------------------------- never raises, returns an error dict
    os.environ.pop("NBA_SQL_SA", None)
    check("disabled when no service account is configured", not t.enabled() and "error" in t.answer("x"))

    # ---------------------------------------------------------------- chat wiring (no LLM, no BigQuery)
    import agent2
    cap = {}

    def fake_run_agent(system, user, tools, impls, max_steps=6, on_event=None):
        cap.update(system=system, names=[x["name"] for x in tools], impls=impls)
        return "stub"
    agent2.common.run_agent = fake_run_agent
    os.environ.pop("NBA_SQL_SA", None)
    r = agent2.ask("q")
    check("chat: ask_data is NOT offered where SQL is disabled", "ask_data" not in cap["names"] and "sql" not in r)
    check("chat: the out-of-scope help text does not promise SQL there", "count or compare" not in cap["system"] and "CAN do" in cap["system"])
    os.environ["NBA_SQL_SA"] = "x@y"
    t.answer = lambda q: {"sql": "SELECT 1 FROM nba_chat.recommendations", "columns": ["n"], "rows": [{"n": i} for i in range(200)], "row_count": 200, "truncated": True}
    agent2.ask("q")
    check("chat: ask_data is offered where enabled, next to the six fixed lookups", "ask_data" in cap["names"] and all(n in cap["names"] for n in ("top_hcps", "explain_hcp", "talking_point", "channel_mix", "segment_summary", "model_info")))
    check("chat: the out-of-scope help text mentions counts and comparisons when SQL is on", "count or compare" in cap["system"])
    f = cap["impls"]["ask_data"]
    a, b, c = f(question="x"), f(question="y"), f(question="z")
    check("chat: the model sees at most 60 rows and a truncation flag", len(a["rows"]) == 60 and a["truncated"])
    check("chat: a second data query is allowed, a third is refused", "rows" in b and "limit" in c.get("error", ""))
    agent2.ask("q")
    check("chat: the per-question counter resets", "rows" in cap["impls"]["ask_data"](question="x"))
    os.environ.pop("NBA_SQL_SA", None)

    # ================================================================ live checks (real identity, real BigQuery)
    if "--live" in sys.argv:
        print("running live checks against BigQuery ...")
        import common
        sa = f"nba-sql-reader@{common.PROJECT}.iam.gserviceaccount.com"
        os.environ["NBA_SQL_SA"] = sa
        ok = t.run_validated("SELECT segment, COUNT(*) AS n FROM nba_chat.recommendations GROUP BY segment ORDER BY n DESC")
        check("live: a real aggregate works", "rows" in ok and len(ok["rows"]) >= 4, ok.get("error"))
        ok2 = t.run_validated("SELECT COUNT(*) AS n FROM nba_chat.recommendations WHERE final_channel = 'call' AND contact_flag")
        check("live: a query containing the value 'call' works", "rows" in ok2 and ok2["rows"][0]["n"] > 0, ok2.get("error"))
        ok3 = t.run_validated("SELECT r.segment, AVG(p.years_in_practice) AS yrs FROM nba_chat.recommendations r JOIN nba_chat.hcp_profile p USING (hcp_id) GROUP BY r.segment")
        check("live: a join across both views works", "rows" in ok3 and len(ok3["rows"]) >= 4, ok3.get("error"))
        big = t.run_validated("SELECT hcp_id FROM nba_chat.recommendations")
        check("live: result rows are capped", "rows" in big and len(big["rows"]) <= t.MAX_ROWS and big["truncated"], big.get("error"))
        for name, q in {"audit log (people's emails)": "SELECT * FROM nba.run_log", "raw data": "SELECT * FROM nba.raw_hcp_master",
                        "published table directly": "SELECT * FROM nba.recommendations_final"}.items():
            r = t.run_validated(q)
            check(f"live: text layer blocks {name}", "error" in r and "rows" not in r)
        # BYPASS the text checks on purpose: send the SQL straight to BigQuery as the restricted account.
        # This proves the identity itself, not just our filters, cannot read anything else.
        for name, q in {"audit log (people's emails)": f"SELECT * FROM `{common.PROJECT}.nba.run_log` LIMIT 1",
                        "raw HCP data": f"SELECT * FROM `{common.PROJECT}.nba.raw_hcp_master` LIMIT 1",
                        "the published table": f"SELECT * FROM `{common.PROJECT}.nba.recommendations_final` LIMIT 1",
                        "demo changes": f"SELECT * FROM `{common.PROJECT}.nba.demo_changes` LIMIT 1"}.items():
            try:
                t._bq("POST", "/queries", {"query": q, "useLegacySql": False, "timeoutMs": 15000})
                check(f"live IAM: restricted account cannot read {name}", False, "the query SUCCEEDED")
            except RuntimeError as e:
                check(f"live IAM: restricted account cannot read {name}", "access denied" in str(e).lower() or "permission" in str(e).lower() or "not found" in str(e).lower(), str(e)[:120])
        try:
            t._bq("POST", "/queries", {"query": f"CREATE TABLE `{common.PROJECT}.{D}.evil` AS SELECT 1 AS x", "useLegacySql": False, "timeoutMs": 15000})
            check("live IAM: restricted account cannot create tables", False, "it SUCCEEDED")
        except RuntimeError as e:
            check("live IAM: restricted account cannot create tables", True)
        try:
            t._bq("POST", "/queries", {"query": f"DELETE FROM `{common.PROJECT}.{D}.recommendations` WHERE TRUE", "useLegacySql": False, "timeoutMs": 15000})
            check("live IAM: restricted account cannot delete", False, "it SUCCEEDED")
        except RuntimeError as e:
            check("live IAM: restricted account cannot delete", True)

    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
