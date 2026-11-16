"""Classify headlines and filings: event type, ESG category, materiality and sentiment.

Rules are explicit so each label can be explained ("classified as a lawsuit because the
headline contains 'class action'"). Sentiment uses a small finance word list written by the
team (not a licensed dictionary). On AWS, set ALPHAFORGE_SENTIMENT=comprehend to score
headlines with Amazon Comprehend instead; the word list stays as the fallback.
"""

from __future__ import annotations

import hashlib
import os
import re

# (event_type, pattern, materiality, esg_category or None, negative event?)
EVENT_RULES: list[tuple[str, str, str, str | None, bool]] = [
    # Specific incidents first, so "pipeline spill prompts regulator probe" is environmental, not governance.
    ("environmental_incident", r"\b(spill\w*|leak\w*|pollut\w+|emission\w* (violation|breach)|explosion|derail\w*|oil sands tailings|methane)\b", "high", "environmental", True),
    ("data_breach", r"\b(data breach|breach(ed)?|hack(ed|ers)?|cyber ?attack\w*|ransomware|outage)\b", "high", "social", True),
    ("recall", r"\b(recall\w*)\b", "high", "social", True),
    ("bankruptcy", r"\b(bankruptcy|chapter 11|insolven\w*|creditor protection)\b", "high", "governance", True),
    ("restatement", r"\b(restate\w*|accounting (error|irregularit\w*)|non-reliance)\b", "high", "governance", True),
    ("investigation", r"\b(probe[sd]?|investigat\w+|subpoena\w*|raid\w*)\b", "high", "governance", True),
    ("regulatory_action", r"\b(fined?|penalt\w+|antitrust|sanctioned|consent order|cease[- ]and[- ]desist|sec charges|settles? with (the )?(sec|ftc|doj|regulators?))\b", "high", "governance", True),
    ("lawsuit", r"\b(lawsuit\w*|sues|sued|class[- ]action|litigation|verdict|jury)\b", "high", "governance", True),
    ("accident", r"\b(crash(es)?|fatal\w*|deaths?|killed|injur\w+|safety (probe|issue|concern)\w*)\b", "medium", "social", True),
    ("labor_dispute", r"\b(strike\w*|walkout|lockout|union\w*|work stoppage)\b", "medium", "social", True),
    ("sanctions", r"\b(sanction\w*|export ban|export controls?|tariff\w*)\b", "medium", None, True),
    ("mna", r"\b(acquir\w+|acquisition\w*|merger\w*|takeover\w*|buyout|to buy|deal for|bid for|stake in)\b", "high", None, False),
    ("guidance_cut", r"\b((cuts?|lowers?|slashe?s?|trims?|withdraws?) (its |annual |full-year )?(forecast|outlook|guidance|target)|profit warning|warns)\b", "high", None, True),
    ("guidance_raise", r"\b((raises?|lifts?|boosts?|ups) (its |annual |full-year )?(forecast|outlook|guidance|target))\b", "medium", None, False),
    ("leadership", r"\b(ceo|chief executive|cfo|chairman|chair) (resign\w*|steps? down|ousted|retir\w+|depart\w*|named|appointed|to step down)|\b(resigns?|steps? down|appoints?|names new)\b", "high", "governance", False),
    ("layoffs", r"\b(layoffs?|job cuts?|cut(s|ting)? jobs|restructur\w+)\b", "medium", "social", True),
    ("impairment", r"\b(impairment|write[- ]?down|writedown|write[- ]off)\b", "high", None, True),
    ("earnings", r"\b(earnings|quarterly (results|profit|revenue|sales)|(first|second|third|fourth)[- ]quarter|q[1-4] (results|profit|revenue|sales|earnings)|profit (rises|falls|jumps|drops|beats|misses)|(beats?|miss(es)?|tops?) (estimates|expectations|forecasts)|eps)\b", "medium", None, False),
    ("rating_change", r"\b(upgrade[sd]?|downgrade[sd]?|price target|rating)\b", "medium", None, False),
    ("capital_return", r"\b(dividend\w*|buyback\w*|share repurchase\w*)\b", "medium", None, False),
    ("product", r"\b(launch\w*|unveil\w*|rolls? out|new (model|product|chip|service)|approval|approved)\b", "low", None, False),
    ("climate", r"\b(climate|carbon|net[- ]zero|greenwashing|esg)\b", "medium", "environmental", False),
    ("monetary_policy", r"\b(rate (cut|hike|decision)|interest rates?|policy rate|inflation|central bank|federal reserve|bank of canada)\b", "medium", None, False),
]
_COMPILED = [(t, re.compile(p, re.IGNORECASE), m, e, n) for t, p, m, e, n in EVENT_RULES]

POSITIVE = set("""beat beats beating record records surge surges surged soar soars soared jump jumps jumped rally rallies rallied
gain gains gained rise rises rising rose climb climbs climbed rebound rebounds rebounded boost boosts boosted
strong stronger strongest robust upgrade upgraded upgrades outperform outperforms profit profits profitable growth grows
grew expand expands expanded expansion exceed exceeds exceeded top tops topped win wins won approval approved raise raises
raised higher high highs best optimism optimistic upbeat bullish breakthrough recovery recovers recovered momentum
dividend buyback surpass surpasses skyrocket skyrockets skyrocketed soaring surging jumping climbing advance advances
advanced""".split())
NEGATIVE = set("""miss misses missed plunge plunges plunged slump slumps slumped tumble tumbles tumbled fall falls fell drop drops
dropped sink sinks sank slide slides slid decline declines declined loss losses lose loses lost weak weaker weakest cut cuts
downgrade downgraded downgrades lawsuit lawsuits sue sues sued probe probes investigation fine fined penalty recall recalls
layoffs warn warns warning warned delay delays delayed halt halts halted bankruptcy default fraud scandal crisis concern
concerns fear fears worry worries risk risks selloff sell-off slowdown slowest lowest low lows bearish volatile volatility
spill leak explosion strike strikes crash crashes breach hack outage sanctions tariff tariffs downturn shortfall writedown
impairment antitrust plummet plummets plummeted tank tanks tanked sinking sliding falling dropping slumping tumbling
wipe wipes wiped erase erases erased lost""".split())

# Headlines in the style of investment newsletters (often syndicated on news sites). They are
# promotional rather than reporting, so they are dropped even when the host site is allow-listed.
PROMO = re.compile(r"\b(should you buy|is it time to buy|time to buy|worth buying|stocks? to (buy|own|watch)|buy now|"
                   r"here's (what|why|how)|investment in|\$[\d,]+ investment|reasons to (buy|sell)|millionaire|"
                   r"top \d+ (stocks?|picks)|could soar|could make you|better buy|no-brainer|motley fool|zacks|"
                   r"buy (the|this) dip|in \d+ years\?|compelling case|income investors|the one i'?d|"
                   r"(buy|sell|hold|outperform|underperform) rating|remains a (buy|hold|sell)|sticks to its|"
                   r"reiterates? (a |its )?(buy|hold|sell)|maintains (a |its )?(buy|hold|sell)|"
                   r"price target (raised|lowered|cut|boosted)|which [\w-]+ stocks? (fits|is better|to)|"
                   r"retirement portfolio|dividend stocks?)", re.IGNORECASE)
# First-person headlines are columns and newsletters, not reporting.
FIRST_PERSON = re.compile(r"(?<![\w'])(I|I'm|I'd|I've|I'll|I’m|I’d|I’ve)(?![\w'])")


def is_promotional(title: str) -> bool:
    return bool(PROMO.search(title) or FIRST_PERSON.search(title) or title.count("!") >= 1)
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "despite"}
WORD = re.compile(r"[a-z][a-z'-]*")

# SEC Form 8-K items: (label, event_type, materiality, esg_category, negative)
EIGHT_K_ITEMS = {
    "1.01": ("Entry into a material agreement", "material_agreement", "high", None, False),
    "1.02": ("Termination of a material agreement", "material_agreement", "high", None, True),
    "1.03": ("Bankruptcy or receivership", "bankruptcy", "high", "governance", True),
    "1.05": ("Material cybersecurity incident", "data_breach", "high", "social", True),
    "2.01": ("Completion of an acquisition or disposition", "mna", "high", None, False),
    "2.02": ("Results of operations (earnings release)", "earnings", "medium", None, False),
    "2.03": ("New direct financial obligation", "financing", "low", None, False),
    "2.04": ("Triggering event accelerating an obligation", "financing", "high", None, True),
    "2.05": ("Exit or restructuring costs", "layoffs", "high", "social", True),
    "2.06": ("Material impairment", "impairment", "high", None, True),
    "3.01": ("Delisting or listing-rule notice", "delisting", "high", "governance", True),
    "3.03": ("Material modification of shareholder rights", "governance_change", "medium", "governance", False),
    "4.01": ("Change of auditor", "governance_change", "medium", "governance", False),
    "4.02": ("Prior financial statements can no longer be relied on", "restatement", "high", "governance", True),
    "5.01": ("Change in control", "mna", "high", None, False),
    "5.02": ("Departure or appointment of directors or officers", "leadership", "medium", "governance", False),
    "5.03": ("Amendment to articles or bylaws", "governance_change", "low", "governance", False),
    "5.07": ("Shareholder vote results", "governance_change", "low", "governance", False),
    "7.01": ("Regulation FD disclosure", "disclosure", "low", None, False),
    "8.01": ("Other events", "other", "low", None, False),
}
PERIODIC_FORMS = {"10-K": "Annual report", "10-Q": "Quarterly report", "40-F": "Annual report (Canadian issuer)",
                  "20-F": "Annual report (foreign issuer)"}
MATERIALITY_RANK = {"low": 0, "medium": 1, "high": 2}


def signal_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def lexicon_sentiment(text: str) -> float:
    words = WORD.findall(text.lower())
    pos = neg = 0
    for i, w in enumerate(words):
        flip = any(x in NEGATORS for x in words[max(0, i - 2):i])
        if w in POSITIVE:
            neg, pos = (neg + 1, pos) if flip else (neg, pos + 1)
        elif w in NEGATIVE:
            pos, neg = (pos + 1, neg) if flip else (pos, neg + 1)
    return 0.0 if pos == neg == 0 else (pos - neg) / (pos + neg + 1)


def classify(title: str, summary: str = "") -> dict:
    text = f"{title} {summary}"
    event, materiality, esg, negative, matched = "general", "low", None, False, None
    for t, rx, m, e, n in _COMPILED:
        hit = rx.search(text)
        if hit:
            event, materiality, esg, negative, matched = t, m, e, n, hit.group(0)
            break
    sentiment = lexicon_sentiment(text)
    if negative and sentiment > -0.2:
        sentiment = min(sentiment, 0.0) - 0.2
    return {"event_type": event, "materiality": materiality, "esg_category": esg,
            "sentiment": round(max(-1.0, min(1.0, sentiment)), 3), "sentiment_method": "lexicon",
            "classification_rule": f"matched '{matched}'" if matched else "no rule matched"}


def severity(materiality: str, sentiment: float) -> int:
    base = {"low": 2, "medium": 3, "high": 4}[materiality]
    return min(5, base + (1 if materiality == "high" and sentiment < -0.3 else 0))


def comprehend_sentiment(signals: list[dict]) -> int:
    """Rescore news headlines with Amazon Comprehend (batches of 25). Returns how many were scored."""
    if os.environ.get("ALPHAFORGE_SENTIMENT") != "comprehend":
        return 0
    import boto3

    client = boto3.client("comprehend")
    todo = [s for s in signals if s.get("kind") == "news" and s.get("sentiment_method") != "amazon_comprehend"]
    done = 0
    for i in range(0, len(todo), 25):
        batch = todo[i:i + 25]
        res = client.batch_detect_sentiment(TextList=[s["title"][:4500] for s in batch], LanguageCode="en")
        for r in res.get("ResultList", []):
            s = batch[r["Index"]]
            sc = r["SentimentScore"]
            s["sentiment"] = round(sc["Positive"] - sc["Negative"], 3)
            s["sentiment_method"] = "amazon_comprehend"
            done += 1
    return done
