"""Compliance filter + public-evidence lookup (Layer 2).

GLUCOREN is a fictional course drug, so approved-claim language is taken from ONE explicitly
named real label as a stand-in: Glumetza (single-ingredient metformin ER, adults) from openFDA,
cross-checked against the same label (by set ID) in DailyMed. Label identifiers are saved in
outputs/public_reference.json. In a real deployment this would be replaced by the GLUCOREN
Prescribing Information.

Everything HCP-facing goes through check_text(): it must (1) contain only the
approved indication claim, (2) carry a fair-balance pointer, (3) avoid
promotional / unsupported language. Nothing here is free-form LLM output.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "outputs" / "public_reference.json"

LABEL_BRAND = "Glumetza"   # single-ingredient metformin ER brand label (adults); see docstring
FALLBACK_CLAIM = ("as an adjunct to diet and exercise to improve glycemic control "
                  "in adults with type 2 diabetes mellitus")
FAIR_BALANCE = "See full Prescribing Information, including Boxed Warning."
PROHIBITED = [
    r"\bbest\b", r"\bsuperior\b", r"\bsafest\b", r"\bcure[sd]?\b", r"\bguarantee[sd]?\b",
    r"no side effects", r"risk[- ]free", r"better than", r"proven to (prevent|reduce)",
    r"weight[- ]loss", r"cardiovascular (benefit|protection)", r"heart[- ]protect",
    r"\bfirst[- ]line for everyone\b", r"\bmiracle\b",
]


def _get(url, timeout=25, retries=4):
    """GET JSON; paced for NCBI (<=3 req/s) and retried on 429."""
    time.sleep(0.4)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-term-project/1.0 (academic)"})
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == retries - 1:
                raise
            time.sleep(3 * (i + 1))


def fetch_public_reference(force=False):
    """Approved claim (openFDA label, cross-checked in DailyMed) + comparator-class evidence (PubMed, CT.gov). Cached locally."""
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text())
    ref = {"approved_claim": FALLBACK_CLAIM, "claim_source": "fallback", "evidence": []}
    # --- Approved claim: ONE explicitly chosen real label, cross-checked in DailyMed ------------------
    # (An earlier version used openFDA's first hit for "metformin", which was a sitagliptin+metformin
    #  combination product -- not a metformin label. Fixed: a named single-ingredient brand label is used.)
    pat = re.compile(r"(as an adjunct to diet and exercise[^.]*type 2 diabetes mellitus)", re.I)
    norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()  # noqa: E731
    try:
        q = urllib.parse.quote(f'openfda.brand_name:"{LABEL_BRAND}"')
        lab = _get(f"https://api.fda.gov/drug/label.json?search={q}&limit=1")["results"][0]
        o = lab["openfda"]
        if o.get("substance_name") != ["METFORMIN HYDROCHLORIDE"]:
            raise ValueError(f"label is not single-ingredient metformin: {o.get('substance_name')}")
        m = pat.search(re.sub(r"\s+", " ", lab["indications_and_usage"][0]))
        if not m:
            raise ValueError("indication sentence not found in label")
        ref["approved_claim"] = m.group(1).strip()
        det = {"label": f"{o['brand_name'][0]} (metformin hydrochloride, single ingredient)",
               "manufacturer": o["manufacturer_name"][0], "set_id": lab["set_id"],
               "effective_time": lab.get("effective_time"), "source": "openFDA drug label API"}
        # cross-check against DailyMed (same set ID)
        try:
            xml = urllib.request.urlopen(urllib.request.Request(
                f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{lab['set_id']}.xml",
                headers={"User-Agent": "nba-term-project/1.0 (academic)"}), timeout=60).read().decode("utf-8", "replace")
            dm = pat.search(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)))
            det["dailymed_crosscheck"] = {"found": bool(dm), "wording": dm.group(1).strip() if dm else None,
                                          "matches_openfda": bool(dm and norm(dm.group(1)) == norm(m.group(1)))}
        except Exception as e:
            det["dailymed_crosscheck"] = {"found": False, "error": str(e)[:120]}
        ref["claim_details"] = det
        agree = det["dailymed_crosscheck"].get("matches_openfda")
        ref["claim_source"] = (f"openFDA label: {det['label']}, {det['manufacturer']}, set {lab['set_id']}; "
                               f"DailyMed cross-check: {'match' if agree else 'NOT confirmed'}")
    except Exception as e:
        ref["claim_note"] = f"label lookup failed, used fallback wording: {e}"
    # --- PubMed: evidence for the NAMED generic comparators (metformin / sulfonylureas) -------------
    # GLUCOREN is fictional, so there is no GLUCOREN-specific literature. This is comparator-class
    # context only and the rationale text says so. Two sources, both verified live against PubMed:
    #   (a) landmark trials, by PMID, kept only if the live title matches what we expect
    #   (b) MeSH-major-topic search (metformin / sulfonylureas AND type 2 diabetes), screened to
    #       glycemic-efficacy and safety titles -- off-label themes (cancer, mortality, ageing,
    #       cognition...) are excluded on purpose: citing them would imply unapproved claims.
    ref["evidence_scope"] = ("Comparator-class context (metformin / sulfonylurea in type 2 diabetes); "
                             "illustrative, not GLUCOREN-specific evidence.")
    LANDMARKS = {"9742977": r"metformin", "9742976": r"sulphonylurea|sulfonylurea"}
    good = re.compile(r"glyc|hba1c|glucose|efficacy|adverse|safety|hypoglyc|lowering", re.I)
    bad = re.compile(r"cancer|surviv|mortality|ageing|aging|cognit|depress|polycystic|thyro|economic|vitamin|"
                     r"adolescen|child|pediatric|pregnan|gestation|osteoarthritis|prevention|prediabet|"
                     r"triple|add-on|versus|insulin|semaglutide|tirzepatide|teneligliptin|protocol", re.I)
    ev = []
    try:
        s = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed"
                 f"&id={','.join(LANDMARKS)}&retmode=json")["result"]
        for pmid, must in LANDMARKS.items():
            if pmid in s and re.search(must, s[pmid]["title"], re.I):
                ev.append({"pmid": pmid, "title": s[pmid]["title"].rstrip("."), "year": s[pmid]["pubdate"][:4],
                           "kind": "landmark trial (verified live)"})
        term = ('("Metformin"[MeSH Major Topic] OR "Sulfonylurea Compounds"[MeSH Major Topic]) AND '
                '"Diabetes Mellitus, Type 2"[MeSH Major Topic] AND ("Meta-Analysis"[Publication Type] OR '
                '"Randomized Controlled Trial"[Publication Type] OR "Systematic Review"[Publication Type]) '
                'AND hasabstract AND ("2012"[dp] : "2026"[dp])')
        ids = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term="
                   f"{urllib.parse.quote(term)}&retmax=100&sort=relevance&retmode=json")["esearchresult"]["idlist"]
        s = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed"
                 f"&id={','.join(ids)}&retmode=json")["result"]
        for i in ids:
            if i in s and good.search(s[i]["title"]) and not bad.search(s[i]["title"]):
                ev.append({"pmid": i, "title": s[i]["title"].rstrip("."), "year": s[i]["pubdate"][:4],
                           "kind": "systematic review / trial (MeSH search)"})
        ref["evidence"] = ev[:5]
    except Exception as e:
        ref["evidence"] = ev
        ref["evidence_note"] = f"PubMed partially unavailable: {e}"
    # --- ClinicalTrials.gov: completed Phase 3/4 trials with posted results, structural filter --------
    # Keep a study only if EVERY listed intervention is metformin / a sulfonylurea / placebo
    # (title words are unreliable: 'on metformin' also matches studies of other drugs).
    DRUG = {"metformin", "glipizide", "glimepiride", "glibenclamide", "glyburide"}
    FILLER = {"hydrochloride", "hcl", "xr", "er", "xl", "extended", "release", "immediate", "tablet", "tablets", "mg",
              "bid", "qd", "placebo", "matching", "to", "of", "and", "or", "the", "dose", "low", "high", "gits", "oral"}

    def only_comparator(name):
        toks = re.findall(r"[a-z]+", name.lower())
        return bool(toks) and all(t in DRUG | FILLER for t in toks) and any(t in DRUG for t in toks)
    try:
        params = {"query.cond": "Type 2 Diabetes", "query.intr": "metformin OR glipizide OR glimepiride",
                  "filter.overallStatus": "COMPLETED",
                  "filter.advanced": "AREA[HasResults]true AND AREA[Phase](PHASE3 OR PHASE4)",
                  "pageSize": 200, "fields": "NCTId,BriefTitle,Phase,InterventionName"}
        trials, scanned, tok = [], 0, None
        for _ in range(8):  # scan up to ~1,600 studies
            q = dict(params)
            if tok:
                q["pageToken"] = tok
            d = _get("https://clinicaltrials.gov/api/v2/studies?" + urllib.parse.urlencode(q))
            for x in d.get("studies", []):
                scanned += 1
                pm = x["protocolSection"]
                ints = [i["name"] for i in pm.get("armsInterventionsModule", {}).get("interventions", [])]
                title = pm["identificationModule"]["briefTitle"]
                if (ints and all(only_comparator(i) or re.match(r"\W*placebo", i, re.I) for i in ints)
                        and good.search(title) and not bad.search(title)
                        and not re.search(r"bioequivalen|puberty|hepatic|double diabetes", title, re.I)):
                    trials.append({"nct_id": pm["identificationModule"]["nctId"], "title": title,
                                   "phase": "/".join(pm.get("designModule", {}).get("phases", []))})
            tok = d.get("nextPageToken")
            if not tok:
                break
        ref["trials"] = trials[:3]
        ref["trials_note"] = (f"Scanned {scanned} completed Phase 3/4 T2D studies with results; "
                              f"{len(trials)} used only the comparator drugs and were on glycemic efficacy.")
    except Exception as e:
        ref["trials_note"] = f"ClinicalTrials.gov unavailable: {e}"
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(ref, indent=2))
    return ref


def check_text(text: str, approved_claim: str, require_claim=True):
    """Return (ok, notes). Deterministic rules only."""
    notes = []
    low = text.lower()
    for pat in PROHIBITED:
        if re.search(pat, low):
            notes.append(f"prohibited language: /{pat}/")
    if require_claim and approved_claim.lower() not in low:
        notes.append("missing approved indication claim")
    if require_claim and "prescribing information" not in low:
        notes.append("missing fair-balance pointer")
    return (len(notes) == 0), "; ".join(notes) if notes else "pass"


def talking_point(approved_claim: str, channel: str) -> str:
    """Pre-approved, channel-appropriate message. No free-form generation."""
    lead = {"call": "Suggested call opener",
            "email": "Suggested approved-email subject line",
            "digital": "Suggested digital placement copy"}.get(channel, "Suggested message")
    return f"{lead}: GLUCOREN is indicated {approved_claim}. {FAIR_BALANCE}"
