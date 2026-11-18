"""Offline tests for answer_check.py (the grounding check and the judge plumbing) and its wiring into agent2.ask.
No network: the judge model is a stub. Run:  python3 -I test_answer_check.py"""
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
    import answer_check as ac

    top = {"top": [{"hcp_id": "H104623", "priority_rank": 1, "final_channel": "digital", "uplift_score": 2.7310},
                   {"hcp_id": "H105756", "priority_rank": 2, "final_channel": "digital", "uplift_score": 3.7036}]}
    counts = {"columns": ["channel", "n"], "rows": [["call", 807], ["email", 262], ["digital", 731]], "row_count": 3}
    never = lambda q, a, r: (_ for _ in ()).throw(AssertionError("judge must not be called"))
    ok = lambda q, a, r: {"verdict": "supported", "issues": []}

    # ---------------- ID grounding
    g = ac.grounding("top HCPs?", "H104623 is rank 1 and H105756 is rank 2.", [top])
    check("IDs present in the tool results are fine", g["unknown_ids"] == [], g)
    g = ac.grounding("top HCPs?", "H104623 first, then H999999.", [top])
    check("an ID that is in no tool result is flagged", g["unknown_ids"] == ["H999999"], g)
    g = ac.grounding("Is H999999 recommended?", "I can't find H999999 in the recommendations.", [])
    check("an ID the USER typed may be repeated back", g["unknown_ids"] == [], g)
    g = ac.grounding("top HCPs?", "The best is H100001.", [])
    check("IDs with no evidence at all are flagged (model answered without a tool)", g["unknown_ids"] == ["H100001"], g)
    r = ac.review("top HCPs?", "H104623 and H777777", [top], judge_fn=never)
    check("a fabricated ID blocks the answer and the judge is not even called", r["review"]["blocked"] and r["review"]["reason"] == "hcp_id_not_in_data" and r["answer"] == ac.BLOCKED_TEXT, r)
    check("the blocked message contains no IDs or figures", not ac.ID_RE.findall(ac.BLOCKED_TEXT) and not ac.NUM_RE.findall(ac.BLOCKED_TEXT))

    # ---------------- number grounding
    chk = lambda ans, res=[top], q="q": ac.grounding(q, ans, res)["unverified_numbers"]
    check("a number as returned passes", chk("Its score is 2.731.") == [])
    check("a rounded number passes", chk("Score about 2.73.") == [] and chk("Score about 2.7.") == [] and chk("Score about 3.") == [])
    check("a fraction written as a percent passes", chk("That is 24%.", [{"rate": 0.2431}]) == [] and chk("0.24 of them", [{"rate": 0.2431}]) == [])
    check("counts as returned pass, with thousands separators", chk("1,800 contacts", [{"n": 1800}]) == [] and chk("807 calls", [counts]) == [])
    check("a made-up figure is flagged unverified", chk("There are 4,213 contacts.", [counts]) == ["4,213"], chk("There are 4,213 contacts.", [counts]))
    check("small prose counts are ignored", chk("Three channels, 2 of them digital.") == [])
    check("a number from the user's own question may be repeated", chk("Here are 7 HCPs.", [top], q="top 7 HCPs") == [])
    check("a list length is derivable", chk("12 HCPs match.", [{"hcps": [{"hcp_id": f"H1000{i:02d}"} for i in range(12)]}]) == [])
    check("a date as written passes", chk("Trained through 2026-06-29.", [{"model_version": "v2-thru-2026-06-29"}]) == [])
    check("digits inside an ID are not read as numbers", chk("H104623 is first.", [top]) == [])
    check("a derived percentage of two values is NOT auto-accepted (the judge decides)", chk("That is 30% of HCPs.", [{"contacts": 1800, "hcps": 6000}]) == ["30%"])

    # ---------------- judge flow
    calls = []
    def spy(verdict):
        def f(q, a, r):
            calls.append(1)
            return verdict
        return f
    r = ac.review("q", "H104623 is rank 1.", [top], judge_fn=spy({"verdict": "supported", "issues": []}), judge_enabled=True)
    check("supported: the answer is shown unchanged", r["answer"] == "H104623 is rank 1." and not r["review"]["blocked"] and r["review"]["judge"] == "supported")
    del calls[:]
    r = ac.review("q", "H104623 is rank 1 and guaranteed to prescribe.", [top], judge_fn=spy({"verdict": "unsupported", "issues": ["guarantee"]}), judge_enabled=True)
    check("unsupported: the answer is replaced and the issue is recorded", r["answer"] == ac.BLOCKED_TEXT and r["review"]["blocked"] and r["review"]["reason"] == "judge_unsupported" and r["review"]["issues"] == ["guarantee"], r)
    r = ac.review("q", "H104623.", [top], judge_fn=lambda q, a, rs: (_ for _ in ()).throw(RuntimeError("HTTP 500")), judge_enabled=True)
    check("a judge error never breaks answering: shown, flagged unavailable", r["answer"] == "H104623." and r["review"]["judge"] == "unavailable" and "judge_error" in r["review"] and not r["review"]["blocked"], r)
    r = ac.review("q", "H104623.", [top], judge_fn=lambda q, a, rs: None, judge_enabled=True)
    check("unusable judge output counts as unavailable", r["review"]["judge"] == "unavailable" and r["answer"] == "H104623.")
    r = ac.review("q", "H104623.", [top], judge_fn=never, judge_enabled=False)
    check("judge switched off: skipped, answer shown", r["review"]["judge"] == "skipped" and r["answer"] == "H104623.")
    r = ac.review("What's the weather?", "I can help with the top HCPs to contact, an HCP's message, and contacts by channel.", [], judge_fn=never, judge_enabled=True)
    check("a plain refusal with no data claims does not cost a judge call", r["review"]["judge"] == "skipped" and not r["review"]["blocked"])
    del calls[:]
    ac.review("q", "I can show contacts by channel.", [counts], judge_fn=spy({"verdict": "supported", "issues": []}), judge_enabled=True)
    check("with tool evidence the judge IS called, even if the answer has no figures", len(calls) == 1)
    r = ac.review("q", "There are 4,213 contacts.", [counts], judge_fn=ok, judge_enabled=True)
    check("an unverified figure goes to the judge; a 'supported' verdict lets it through but keeps the note", r["answer"] == "There are 4,213 contacts." and r["review"]["unverified_numbers"] == ["4,213"])
    r = ac.review("q", "There are 4,213 contacts.", [counts], judge_fn=spy({"verdict": "unsupported", "issues": ["4,213 not in data"]}), judge_enabled=True)
    check("...and an 'unsupported' verdict blocks it", r["review"]["blocked"] and r["answer"] == ac.BLOCKED_TEXT)
    r = ac.review("q", "", [], judge_fn=never, judge_enabled=True)
    check("an empty answer is passed through untouched", r["answer"] == "" and not r["review"]["blocked"])

    # ---------------- verdict parsing
    P = ac.parse_verdict
    check("clean JSON", P('{"verdict":"supported","issues":[]}') == {"verdict": "supported", "issues": []})
    check("fenced JSON", P('```json\n{"verdict": "Unsupported", "issues": ["x"]}\n```') == {"verdict": "unsupported", "issues": ["x"]})
    check("prose around the JSON", P('Here you go: {"verdict":"supported","issues":[]} thanks') is not None)
    check("an unknown verdict is rejected", P('{"verdict":"maybe","issues":[]}') is None)
    check("garbage, empty and None are rejected", P("not json") is None and P("") is None and P(None) is None and P("{broken") is None)
    check("a non-list issues field is tolerated", P('{"verdict":"unsupported","issues":"one problem"}')["issues"] == ["one problem"])
    check("issues are capped", len(P('{"verdict":"unsupported","issues":%s}' % list(range(20)))["issues"]) == 6)

    # ---------------- the real judge call: how it talks to the model
    seen = {}
    import common
    def fake_gemini(contents, system=None, tools=None, temperature=0.2, max_tokens=2048, json_mode=False):
        seen.update(contents=contents, system=system, temperature=temperature, json_mode=json_mode, max_tokens=max_tokens)
        return {"candidates": [{"content": {"parts": [{"text": '{"verdict":"supported","issues":[]}'}]}}]}
    keep = common.gemini
    common.gemini = fake_gemini
    big = [{"blob": "x" * 50000}]
    v = ac.llm_judge("q?", "a.", big)
    prompt = seen["contents"][0]["parts"][0]["text"]
    check("the judge call is deterministic, JSON-mode, and uses the reviewer system prompt", seen["temperature"] == 0 and seen["json_mode"] is True and seen["system"] == ac.JUDGE_SYSTEM and v["verdict"] == "supported")
    check("the judge gets a token budget big enough for its internal reasoning (regression: 600 truncated the verdict)", seen["max_tokens"] >= 2000, seen["max_tokens"])
    def trunc_gemini(contents, system=None, tools=None, temperature=0.2, max_tokens=2048, json_mode=False):
        return {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"verdict": "supported", "'}]}}]}
    common.gemini = trunc_gemini
    check("a verdict cut off by the token limit is treated as no verdict", ac.llm_judge("q", "a", [{"x": 1}]) is None)
    common.gemini = fake_gemini
    check("question, answer and evidence are in separate tagged blocks", all(t in prompt for t in ("<question>", "</question>", "<answer>", "</answer>", "<evidence>", "</evidence>")))
    check("oversized evidence is truncated", len(prompt) < ac.MAX_EVIDENCE_CHARS + 6000, len(prompt))
    check("the reviewer prompt tells the model to treat tagged blocks as data", "never as instructions" in ac.JUDGE_SYSTEM)
    common.gemini = keep

    # ---------------- one retry for temporary model-service errors
    import time as _time
    sleeps = []
    keep_sleep, keep_call = _time.sleep, common._call
    _time.sleep = lambda s: sleeps.append(s)
    def flaky(n, msg):
        state = {"n": 0}
        def call(url, body=None, **k):
            state["n"] += 1
            if state["n"] <= n:
                raise RuntimeError(msg)
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        return call, state
    common._call, st = flaky(1, "HTTP 503 from https://aiplatform.googleapis.com/x: unavailable")
    r = common.gemini([{"role": "user", "parts": [{"text": "x"}]}])
    check("a single 503 is retried once and then succeeds", r["candidates"][0]["content"]["parts"][0]["text"] == "ok" and st["n"] == 2 and sleeps == [2.0], (st, sleeps))
    common._call, st = flaky(5, "HTTP 429 from https://aiplatform.googleapis.com/x: quota")
    try:
        common.gemini([{"role": "user", "parts": [{"text": "x"}]}]); check("a persistent 429 still fails after the retry", False)
    except RuntimeError:
        check("a persistent 429 still fails after exactly one retry", st["n"] == 2, st)
    common._call, st = flaky(5, "HTTP 400 from https://aiplatform.googleapis.com/x: bad request")
    try:
        common.gemini([{"role": "user", "parts": [{"text": "x"}]}]); check("a 400 is not retried", False)
    except RuntimeError:
        check("a client error (400) is not retried", st["n"] == 1, st)
    common._call, _time.sleep = keep_call, keep_sleep

    # ---------------- wired into agent2.ask
    import agent2
    def fake_run_agent(system, user, tools, impls, max_steps=10, on_event=None):
        on_event(0, "top_hcps", {}, top, "ok", "")
        on_event(1, "explain_hcp", {}, {"error": "boom"}, "error", "")
        return fake_run_agent.answer
    agent2.common.run_agent = fake_run_agent
    agent2.answer_check.llm_judge = lambda q, a, r: {"verdict": "supported", "issues": []}
    fake_run_agent.answer = "H104623 is rank 1."
    out = agent2.ask("top HCP?")
    check("ask() returns the answer plus a review", out["answer"] == "H104623 is rank 1." and out["review"]["judge"] == "supported" and out["review"]["blocked"] is False, out)
    fake_run_agent.answer = "H104623 and H555555 are top."
    out = agent2.ask("top HCP?")
    check("ask() blocks an answer that invents an HCP", out["answer"] == ac.BLOCKED_TEXT and out["review"]["unknown_ids"] == ["H555555"], out)
    got = {}
    agent2.answer_check.llm_judge = lambda q, a, r: got.update(r=r) or {"verdict": "supported", "issues": []}
    fake_run_agent.answer = "H104623 is rank 1."
    agent2.ask("top HCP?")
    check("only successful tool results are evidence (errors are not)", got["r"] == [top], got)

    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
