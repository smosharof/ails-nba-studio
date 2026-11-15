"""The 'recommendations' status line: pending only when source data changed after the last publish AND after the last
Layer 2 gate check. Run:  python3 -I test_recs_freshness.py"""
import sys
sys.path.insert(0, ".")
PASSES, FAILS = 0, []


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print("  FAIL:", name, detail)


def main():
    import workflow as w
    T = "2026-11-15 12:00:00"
    check("source older than the publish: current", w.recs_status("2026-11-14 23:00:00", "2026-11-14 23:46:16", None) == "current")
    check("source changed after the publish: pending", w.recs_status("2026-11-15 21:26:52", "2026-11-14 23:46:16", None) == "pending")
    check("a Layer 2 gate that decided SKIP after the change clears it", w.recs_status("2026-11-15 21:26:52", "2026-11-14 23:46:16", "2026-11-15 21:40:00") == "current")
    check("a gate check from BEFORE the change does not clear it", w.recs_status("2026-11-15 21:26:52", "2026-11-14 23:46:16", "2026-11-15 10:00:00") == "pending")
    check("missing times: unknown, never a false alarm", w.recs_status(None, T, None) == "unknown" and w.recs_status(T, None, None) == "unknown")
    check("same instant counts as current", w.recs_status(T, T, None) == "current")
    sqls = []
    w.common.bq_query = lambda sql: sqls.append(sql) or [{"src": "2026-11-15 21:26:52", "pub": "2026-11-14 23:46:16", "g2": None}]
    r = w.recs_freshness()
    check("recs_freshness returns the status and both times", r == {"status": "pending", "published_at": "2026-11-14 23:46:16", "source_changed_at": "2026-11-15 21:26:52"}, r)
    check("it is one read-only query", len(sqls) == 1 and sqls[0].lstrip().startswith("SELECT") and "INSERT" not in sqls[0] and "CREATE" not in sqls[0])
    check("it looks at raw_* tables and wording overrides only as sources", "STARTS_WITH(table_id, 'raw_')" in sqls[0] and "demo_overrides" in sqls[0])
    def boom(sql): raise RuntimeError("bq down")
    w.common.bq_query = boom
    check("if the check fails the status is unknown, not an error", w.recs_freshness()["status"] == "unknown")
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
