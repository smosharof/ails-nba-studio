"""ensure_tables() must create the bookkeeping tables once per process, not on every page refresh (BigQuery limits table
updates to about 5 per 10 seconds per table). Run:  python3 -I test_demo_ensure.py"""
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
    import demo
    calls = []
    demo.common.bq_query = lambda sql: calls.append(sql) or []
    demo.ensure_tables()
    check("first call creates both tables", len(calls) == 2 and all("CREATE TABLE IF NOT EXISTS" in c for c in calls), calls)
    for _ in range(25):
        demo.ensure_tables()
    check("25 more calls create nothing", len(calls) == 2, len(calls))
    del calls[:]
    for _ in range(20):
        demo.active_changes()
    check("20 status loads run only the two SELECTs each (no DDL)", len(calls) == 40 and not any("CREATE" in c for c in calls), len(calls))
    # a failed first attempt must be retried, not remembered as done
    demo._TABLES_READY = False
    def boom(sql): raise RuntimeError("bq down")
    demo.common.bq_query = boom
    try:
        demo.ensure_tables(); check("a failed attempt raises", False)
    except RuntimeError:
        check("a failed attempt raises", True)
    check("...and is not remembered as done", demo._TABLES_READY is False)
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
