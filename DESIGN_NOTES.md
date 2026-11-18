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

## Checking answers before they are shown

The Recommender reads from fixed tools and a read-only SQL tool, and a prompt tells it never to invent anything. A prompt is not a guarantee, so every answer goes through two independent checks in `answer_check.py` before it reaches the user:

1. **Grounding check (deterministic, free).** Every HCP ID in the answer must appear in what the tools returned, or in the user's own question. An ID found nowhere is a fabrication, and the answer is blocked without even asking the judge. Figures are compared too, allowing for rounding, percent forms of fractions, list lengths and numbers the user typed. A figure that cannot be found is not blocked by itself (a percentage of two returned values is legitimate); it is handed to the judge.
2. **LLM judge (a second Gemini call, temperature 0, JSON output).** It sees the question, the answer and the raw tool outputs, each in its own tagged block that it is told to treat as data, and rules whether every claim is supported. It also flags invented promotional wording, guaranteed outcomes, and PubMed references presented as evidence about GLUCOREN. An `unsupported` verdict replaces the answer with a short, safe message and writes an `ask_blocked` row to `run_log`.

Design choices worth knowing:
- **Fail closed on content, fail open on the reviewer.** A bad answer is never shown. But if the judge call itself errors or returns something unusable, the answer is shown and the review records `judge: unavailable`: an outage of the reviewer must not take the Recommender down, and the ID check has already run.
- **The judge costs one extra model call per answer that carries data claims.** A plain refusal (no tool results, no IDs, no figures) skips it. Set `NBA_ANSWER_JUDGE=0` to turn it off.
- **What it does not do.** There is still no human approval step before a field rep acts on an answer. Promotional wording is never generated by the model: it comes from one pre-approved label and passes the compliance filter, which fails closed.

`test_answer_check.py` tests the rules offline (including mutation checks: disabling the ID check or ignoring the verdict makes tests fail). `test_golden.py` is the end-to-end measure: 26 questions with answers computed from the live data (top HCPs by channel and segment, counts, averages, an approved message, off-topic and adversarial prompts such as a request to call GLUCOREN "the best diabetes drug" or to drop a table).

### What the golden set found (Nov 2026)

The first live runs were worth more than the offline tests:
- **A bug in my own judge call.** Gemini 3 spends about 500 tokens on internal reasoning, and the judge was allowed 600 in total, so on longer evidence the JSON verdict was cut off and counted as "judge unavailable": about one answer in five went out unreviewed. Fixed (4,000-token budget, a truncated verdict is explicitly "no verdict"), with regression tests.
- **The judge caught real problems once it worked.** Three answers cited the overstatement range "1.6x to 2.5x" from the assistant's prompt while the tool's own output did not contain it, and one invented a reason for channel switches ("due to frequency caps or consent") that no data returned. Fixes: the caveat summary is now part of what `model_info` returns, and the prompt says to give a reason only if a tool returned it. Another answer called an HCP "rank 3" (its position in a channel list) where the data says priority rank 4; the prompt now says to show the rank exactly as returned.
- **Several first-run "failures" were my own criteria being brittle** (a refusal repeating the user's words, "not a guaranteed outcome" matching "guaranteed"). Criteria that match on text must tolerate negation and echo.
- **A transient model-service error** surfaced once as a failed answer. `common.gemini` now retries once on 429 and 5xx.

Last full run: 26 of 26 passed (21 judged "supported", 5 refusals that skip the judge, 0 blocked). Results vary a little between runs because the model is not deterministic, so treat the golden set as a regression signal, not a proof. It checks facts computed from the live data; it is not a human review of every possible question.
