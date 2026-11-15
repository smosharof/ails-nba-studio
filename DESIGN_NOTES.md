# Design notes

Short write-ups of decisions and incidents behind the console. The README is the guide; this is the "why".

## BigQuery counts `CREATE TABLE IF NOT EXISTS` as a table update

BigQuery allows only about 5 table-changing operations per 10 seconds per table, and it counts DDL even when the table already exists. `demo.ensure_tables()` ran on every status or options load, so a burst of page loads (two windows, refresh on focus, polling during a run) made the status card fail with "Job exceeded rate limits: Your table exceeded quota for table update operations".

Fix: create `nba.demo_changes` and `nba.demo_overrides` once per server process (nothing ever drops them), and make a failed background refresh keep the status card as it was instead of replacing it with the error. Tests: `test_demo_ensure.py`.

## Re-attaching to a run

Runs live in the server's memory, not in the page. `/api/state` reports `active_run` (the id of a run that is running or paused at a gate). A reloaded page, or a second window, uses it to redraw Watch Logs, including the Continue and Abort buttons. While a run is active the status line shows **Run in progress** or **Paused at Gate N** instead of the freshness verdict. When a run pauses, Watch Logs scrolls into view and the tab title becomes "Paused · Trainer · NBA Studio". A paused run is abandoned after 20 minutes. Starting a run while another is active is refused, and the message links to the active one.

## The "Recommendations have pending changes" rule

The status card has two lines. **Model** answers Layer 1's question: has the model seen the latest outcomes? **Recommendations** answers Layer 2's: it shows *pending changes* when a source table Layer 2 reads (any `raw_*` table, or the wording overrides) was modified after both the last publish of `recommendations_final` and the last time Layer 2's gate looked (`run_log`, step `layer2_gate`; a gate that decided SKIP counts as having looked, so a harmless change doesn't leave a warning stuck).

It is a metadata check (`workflow.recs_freshness`, pure rule in `recs_status`, tests in `test_recs_freshness.py`). It does not recompute Layer 2, so it means "the source data changed", not "N recommendations will change". Preview gives the exact answer.

## Two windows

The gear and "Manage in Admin" open Admin in a named window (`nba-admin`). Each page refreshes its status, tables and options when its window regains focus (at most every 3 seconds; a failed refresh is silent), and immediately when another window of the same browser finishes an apply, a reset or a run, using a `BroadcastChannel` (no polling). A window that did not start a running task shows it ("Working: reset is still running.") from the server's busy flag. A change applied in Admin while a Manual run is paused at Gate 2 makes the paused gate re-check by itself.

## Hidden tables

The console lists and previews tables of the `nba` dataset, except `run_log`, `demo_changes` and `demo_overrides` (`workflow.HIDDEN_TABLES`). The server omits them from the list and refuses their previews, so hiding does not depend on the page. If a sensitive table were ever un-hidden, previews would still select only whitelisted columns (`RESTRICTED_TABLES`). Views report 0 rows in BigQuery metadata, so row counts for views are queried.
