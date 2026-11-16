"""Ask AlphaForge reasoning: a deterministic agent that plans, gathers evidence and answers.

Every answer is built in visible steps:
  1. Understand the question (intent, assets, amounts, dates).
  2. Read the investor profile (tolerances, needs, weights that apply).
  3. Gather market evidence (features from asset_features.json).
  4. Gather news evidence (filings, allow-listed articles, policy decisions) for the right dates.
  5. Evaluate fit and run the governance checks.
  6. Challenge the result (tensions, missing data, uncertainty).
  7. Answer, citing each fact as [n] against the evidence list.

Steps report what was looked at and which rule decided, not hidden model reasoning. Each
step lists the evidence references it relies on, and the trace lists each tool call.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .data.provider import DataProvider
from .governance import check_proposal
from .portfolio import stress_test, weights_from_holdings
from .profile_features import COMPONENTS
from .profiles import profile_assumptions
from .scoring import ScoringContext, score_asset, score_universe
from .suggestions import build_suggestions

PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|percent\b|per cent\b|pour ?cent\b)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20[12]\d)\b")
DATE_RE = re.compile(r"\b(20[12]\d-\d{2}-\d{2})\b")
MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), 1)}
MOVE_WORDS = ("why did", "what happened", "fall", "fell", "drop", "dropped", "crash", "plunge", "plunged", "jump",
              "jumped", "surge", "surged", "rally", "rallied", "spike", "rose", "tumble", "sink", "sank", "soar", "move")
NEWS_WORDS = ("news", "headline", "latest", "happening", "announce", "filing", "controvers", "event", "media", "press")
RANK_WORDS = ("best", "top", "rank", "which asset", "which stock", "which fund", "what should i", "recommend", "fit me",
              "fits me", "good for me", "suit", "match me", "options for me", "shortlist")
MARKET_WORDS = ("market", "regime", "interest rate", "rates", "bank of canada", "federal reserve", "the fed", "inflation",
                "macro", "economy", "yield")
STRESS_WORDS = ("2022", "stress", "crash", "scenario", "krach", "downturn", "recession")
PORTFOLIO_WORDS = ("diversif", "portfolio", "rebalanc", "suggest", "improve", "radar", "portefeuille", "holdings")


# ----- evidence registry and helpers ---------------------------------------------------
class Evidence:
    def __init__(self):
        self.items: list[dict] = []
        self._index: dict[tuple, int] = {}

    def add(self, claim: str, value, source_id: str, period: str | None = None, unit: str | None = None,
            kind: str = "data") -> int:
        key = (claim, repr(value), source_id)
        if key in self._index:
            return self._index[key]
        ref = len(self.items) + 1
        self.items.append({"ref": ref, "claim": claim, "value": value, "source_id": source_id, "period": period,
                           "unit": unit, "kind": kind})
        self._index[key] = ref
        return ref

    def add_item(self, e: dict, kind: str = "data") -> int:
        k = kind if kind != "data" else ("news" if str(e.get("source_id", "")).startswith("http") else "data")
        return self.add(e["claim"], e.get("value"), e.get("source_id") or "", e.get("period"), e.get("unit"), k)


def cite(*refs) -> str:
    refs = sorted({r for r in refs if r})
    return "".join(f"[{r}]" for r in refs)


def pct(x, d=0) -> str:
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def spct(x, d=0) -> str:
    return "n/a" if x is None else f"{x * 100:+.{d}f}%"


def _aliases(provider: DataProvider) -> list[tuple[str, str]]:
    out = []
    for t, a in provider.assets.items():
        out.append((t.lower(), t))
        base = t.split(".")[0].split("-")[0].lower()
        if len(base) >= 3 or t in ("PG", "TD.TO", "RY.TO"):
            out.append((base, t))
        first = re.sub(r"[^a-z0-9 ]", "", a["name"].lower()).split()[0]
        if len(first) >= 4 and first not in ("canadian", "ishares", "spdr", "invesco", "vanguard", "toronto", "royal",
                                             "alimentation", "bmo"):
            out.append((first, t))
    out += [("bitcoin", "BTC-USD"), ("ether", "ETH-USD"), ("ethereum", "ETH-USD"), ("google", "GOOGL"),
            ("royal bank", "RY.TO"), ("couche-tard", "ATD.TO"), ("s&p 500", "SPY"), ("nasdaq", "QQQ"), ("gold", "GLD"),
            ("cn rail", "CNR.TO"), ("j&j", "JNJ"), ("exxon", "XOM"), ("jpmorgan", "JPM")]
    return sorted(out, key=lambda x: -len(x[0]))


def find_tickers(question: str, provider: DataProvider) -> list[str]:
    q = " " + question.lower() + " "
    found: list[str] = []
    for alias, t in _aliases(provider):
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", q) and t not in found:
            found.append(t)
            q = q.replace(alias, " ")
    return found


def parse_period(question: str, as_of: str) -> tuple[str, str, str] | None:
    """(start, end, label) for a date reference in the question, if any."""
    q = question.lower()
    m = DATE_RE.search(q)
    if m:
        d = date.fromisoformat(m.group(1))
        return (d - timedelta(days=3)).isoformat(), (d + timedelta(days=3)).isoformat(), m.group(1)
    end = date.fromisoformat(as_of)
    if "last month" in q or "past month" in q:
        return (end - timedelta(days=31)).isoformat(), as_of, "the last month"
    if "last week" in q or "past week" in q or "this week" in q:
        return (end - timedelta(days=8)).isoformat(), as_of, "the last week"
    if "this year" in q or "year to date" in q or "ytd" in q:
        return f"{end.year}-01-01", as_of, f"{end.year} to date"
    if "recent" in q or "lately" in q:
        return (end - timedelta(days=60)).isoformat(), as_of, "the last 60 days"
    y = YEAR_RE.search(q)
    for name, num in MONTHS.items():
        if re.search(rf"\b{name}\b", q):
            year = int(y.group(1)) if y else (end.year if num <= end.month else end.year - 1)
            start = date(year, num, 1)
            stop = date(year + (num == 12), num % 12 + 1, 1) - timedelta(days=1)
            return start.isoformat(), stop.isoformat(), f"{name.capitalize()} {year}"
    if y:
        return f"{y.group(1)}-01-01", f"{y.group(1)}-12-31", y.group(1)
    return None


def classify_intent(question: str, tickers: list[str], has_pct: bool) -> str:
    q = question.lower()
    if tickers and has_pct:
        return "allocation"
    if tickers and any(w in q for w in MOVE_WORDS) and ("why" in q or "what happened" in q or "explain" in q):
        return "explain_move"
    if any(w in q for w in NEWS_WORDS):
        return "news"
    if len(tickers) >= 2 or (tickers and any(w in q for w in (" vs ", "versus", "compare"))):
        return "compare"
    if tickers:
        return "asset"
    if any(w in q for w in RANK_WORDS):
        return "ranking"
    if any(w in q for w in STRESS_WORDS):
        return "stress"
    if any(w in q for w in MARKET_WORDS):
        return "market"
    if any(w in q for w in PORTFOLIO_WORDS):
        return "portfolio"
    return "help"


INTENT_LABELS = {
    "allocation": "a request to add a position (governance check required)",
    "explain_move": "an explanation of a past price move",
    "news": "recent news and filings",
    "compare": "a comparison of assets for this profile",
    "asset": "how one asset fits this profile",
    "ranking": "which assets fit this profile best",
    "stress": "a historical stress scenario",
    "market": "current market and rate conditions",
    "portfolio": "a review of the portfolio",
    "help": "not recognised",
}


class Reasoner:
    def __init__(self, question: str, profile: dict, provider: DataProvider, window: str | None):
        self.q, self.profile, self.provider, self.window = question, profile, provider, window
        self.ev = Evidence()
        self.steps: list[dict] = []
        self.trace: list[dict] = []
        self.limitations: list[str] = []
        self.assumptions: list[str] = []
        self.news_cited: list[dict] = []
        self.ctx = ScoringContext(profile, provider, window)
        self.inv = self.ctx.inv
        self.as_of = self.ctx.as_of
        self.extra: dict = {}

    # ----- recording ----------------------------------------------------------------
    def step(self, title: str, summary: str, details: list[str] | None = None, refs: list[int] | None = None,
             tool: str | None = None) -> None:
        self.steps.append({"step": len(self.steps) + 1, "title": title, "summary": summary,
                           "details": details or [], "refs": sorted({r for r in (refs or []) if r}), "tool": tool})

    def tool(self, name: str, inp: dict, out) -> None:
        self.trace.append({"step": len(self.trace) + 1, "tool": name, "input": inp, "output": out})

    def feat(self, t: str, key: str, claim: str | None = None):
        """Feature value and evidence ref (None, None if missing)."""
        v = self.provider.feature(t, key) if self.ctx.has_features else None
        if v is None:
            return None, None
        d = self.provider.feature_definitions().get(key, {})
        ref = self.ev.add(f"{t}: {claim or d.get('label', key)}", v, self.provider.feature_ref(t, key),
                          self.ctx.feature_period, d.get("unit"))
        return v, ref

    def cite_signal(self, s: dict) -> int:
        kind = {"filing": "filing", "news": "news", "macro": "macro", "official": "official"}.get(s.get("kind"), "news")
        ref = self.ev.add(s["title"], s.get("sentiment") if s.get("kind") == "news" else s.get("event_type"),
                          s.get("source_url") or f"signals.json#{s['signal_id']}",
                          f"{s.get('publisher')} · {(s.get('published_at') or '')[:10]}",
                          "sentiment" if s.get("kind") == "news" else "label", kind)
        if not any(n["signal_id"] == s["signal_id"] for n in self.news_cited):
            self.news_cited.append({k: s.get(k) for k in ("signal_id", "title", "publisher", "published_at", "source_url",
                                                          "kind", "event_type", "materiality", "sentiment", "source_tier",
                                                          "linked_move")} | {"ref": ref})
        return ref

    # ----- reusable steps -----------------------------------------------------------------
    def profile_step(self, focus: str = "") -> None:
        inv = self.inv
        lo, hi = inv["vol_band"]
        w = inv["weights"]
        top = sorted(w, key=lambda k: -w[k])[:3]
        needs = [n for n, on in (("income", inv["needs"]["income"]), ("established companies", inv["needs"]["quality"]),
                                 ("stability", inv["needs"]["stability"])) if on]
        summary = (f"{inv['risk_label']} risk, {inv['horizon_class']} horizon ({inv['horizon_years']} years), "
                   f"{inv['objective']} objective. Volatility band {lo:.0%}-{hi:.0%}; loss tolerance "
                   f"{inv['drawdown_tolerance']:.0%}, set by the {inv['drawdown_tolerance_source']}.")
        details = [f"Largest weights for this profile: " + ", ".join(f"{COMPONENTS[k].lower()} {w[k]:.0%}" for k in top) + "."]
        if needs:
            details.append("Stated needs: " + ", ".join(needs) + ".")
        if inv["needs"]["esg"] != "none":
            details.append(f"ESG sensitivity: {inv['needs']['esg']}; exclusions: {', '.join(inv['esg_exclusions']) or 'none'}.")
        details.append(f"Crypto preference: {inv['crypto']}; speculation aversion: {inv['speculation_aversion']}.")
        if focus:
            details.append(focus)
        self.tool("get_profile_features", {}, {"risk_level": inv["risk_level"], "horizon_class": inv["horizon_class"],
                                               "weights": w})
        self.step("Read the investor profile", summary, details, tool="get_profile_features")
        self.assumptions += profile_assumptions(self.profile)

    def market_step(self, tickers: list[str]) -> dict[str, dict]:
        out = {}
        details, refs = [], []
        if not self.ctx.has_features:
            self.limitations.append("Feature engine output is not loaded; only window metrics are available.")
        for t in tickers:
            vals = {}
            parts = []
            for key, label, fmt in (("ret_1y", "1-year return", spct), ("vol_1y", "1-year volatility", pct),
                                    ("garch_vol_1m", "next-month volatility forecast", pct),
                                    ("mdd_5y", "worst fall in 5 years", pct), ("stress_2022", "2022 return", spct),
                                    ("sharpe_3y", "3-year Sharpe", lambda x: f"{x:.2f}"),
                                    ("px_vs_sma_200", "vs 200-day average", spct), ("rsi_14", "RSI", lambda x: f"{x:.0f}"),
                                    ("dividend_yield_ttm", "dividend yield", lambda x: pct(x, 1)),
                                    ("pe", "P/E", lambda x: f"{x:.0f}")):
                v, r = self.feat(t, key)
                if v is None:
                    if key == "vol_1y":
                        m = self.provider.metric(t, self.ctx.window, "ann_vol")
                        if m is not None:
                            v, r = m, self.ev.add(f"{t}: volatility ({self.ctx.window})", m,
                                                  self.provider.evidence_ref(t, self.ctx.window, "ann_vol"), self.ctx.period, "fraction")
                    if v is None:
                        continue
                vals[key] = v
                refs.append(r)
                parts.append(f"{label} {fmt(v)} {cite(r)}")
            out[t] = vals
            if parts:
                details.append(f"{t}: " + ", ".join(parts) + ".")
            missing = self.provider.feature_missing(t) if self.ctx.has_features else {}
            if missing.get("pe") and self.provider.assets[t]["asset_group"] == "equity":
                details.append(f"{t}: P/E not available ({missing['pe']})")
        self.tool("get_asset_features", {"tickers": tickers}, {t: {k: v for k, v in vals.items()} for t, vals in out.items()})
        th = self.provider.data_through
        summary = (f"Read features for {', '.join(tickers)} as of {self.as_of}"
                   + (f" (starter data to {th.get('starter')}, live data to {th.get('live')})." if th.get("live") else "."))
        self.step("Gather market evidence", summary, details, refs, tool="get_asset_features")
        return out

    def news_step(self, tickers: list[str], start: str, end: str, label: str, max_items: int = 5,
                  include_macro: bool = False) -> list[dict]:
        items = self.provider.signals_between(tickers, start, end)
        news = [s for s in items if s.get("kind") == "news"]
        filings = [s for s in items if s.get("kind") == "filing" and s.get("materiality") != "low"]
        macro = self.provider.macro_signals(start, end) if include_macro else []
        material = [s for s in items if s.get("materiality") == "high"]
        ranked = sorted(news, key=lambda s: ({"high": 0, "medium": 1, "low": 2}.get(s.get("materiality"), 3),
                                             s.get("published_at", "")), reverse=False)
        picked = (material[:2] + [s for s in ranked if s not in material] + filings)[:max_items]
        picked = sorted({s["signal_id"]: s for s in picked}.values(), key=lambda s: s["published_at"], reverse=True)
        refs = [self.cite_signal(s) for s in picked]
        mrefs = [self.cite_signal(s) for s in macro[:3]]
        details = [f"{s['published_at'][:10]} · {s.get('publisher')}: \"{s['title']}\" "
                   f"({s.get('event_type', 'general').replace('_', ' ')}, {s.get('materiality')} materiality"
                   + (f", sentiment {float(s.get('sentiment') or 0):+.2f}" if s.get("kind") == "news" else "") + f") {cite(r)}"
                   for s, r in zip(picked, refs)]
        details += [f"{s['published_at'][:10]} · {s.get('publisher')}: {s['title']} {cite(r)}" for s, r in zip(macro[:3], mrefs)]
        sent = None
        if news:
            sent = sum(float(s.get("sentiment") or 0) for s in news) / len(news)
        summary = (f"Searched filings (SEC EDGAR) and allow-listed outlets for {', '.join(tickers) if tickers else 'the market'} "
                   f"over {label} ({start} to {end}): {len(news)} article(s), {len([s for s in items if s.get('kind') == 'filing'])} "
                   f"filing(s), {len(material)} high-materiality item(s)"
                   + (f", average headline sentiment {sent:+.2f}" if sent is not None else "") + "."
                   + (f" {len(macro)} policy decision or official statement(s) in the same window." if include_macro else ""))
        if not items and not macro:
            summary += " No coverage found; that is not evidence that nothing happened."
        self.tool("get_news", {"tickers": tickers, "start": start, "end": end},
                  {"articles": len(news), "filings": len(filings), "material": len(material), "macro": len(macro)})
        self.step("Gather news evidence", summary, details, refs + mrefs, tool="get_news")
        self.extra.setdefault("news_window", {"start": start, "end": end})
        # Most important first: high materiality, then news over filings, then newest.
        rank = {"high": 0, "medium": 1, "low": 2}
        return sorted(picked, key=lambda s: (rank.get(s.get("materiality"), 3), s.get("kind") != "news",
                                             "".join(chr(255 - ord(c)) for c in s.get("published_at", ""))))

    def fit_step(self, t: str, rank_of: tuple[int, int] | None = None) -> dict:
        r = score_asset(t, self.ctx)
        refs = []
        details = []
        for c in sorted((c for c in r["components"] if c["applicable"]), key=lambda c: -c["weight"]):
            for e in c["evidence"][:2]:
                refs.append(self.ev.add_item(e))
            details.append(f"{c['label']}: {c['score']:.0f}/100 x {c['weight']:.0%} weight = {c['contribution']:.1f} pts. {c['explanation']}")
        for c in r["components"]:
            if not c["applicable"]:
                details.append(f"{c['label']}: left out ({c['explanation']}); its weight is shared among the others.")
        details += r["gates"]
        rank_txt = f", rank {rank_of[0]} of {rank_of[1]}" if rank_of else ""
        self.tool("score_asset", {"ticker": t}, {"score": r["score"], "verdict": r["verdict"]["key"], "status": r["status"]})
        self.step(f"Evaluate fit: {t}", f"{r['name']} scores {r['score']}/100 ({r['verdict']['label'].lower()}{rank_txt}); "
                                         f"status {r['status'].replace('_', ' ')}.", details, refs, tool="score_asset")
        return r

    def finish(self, intent: str, answer: str, confidence: dict, **extra) -> dict:
        if self.provider.is_placeholder:
            self.limitations.append("Metrics are placeholder values, not pipeline output.")
        self.step("Answer", "Wrote the answer from the steps above; each figure cites its evidence number.",
                  [f"Confidence: {confidence['level']}."] + [f"Reason: {x}" for x in confidence.get("reasons", [])[:4]])
        return {"intent": intent, "answer": answer, "evidence": self.ev.items, "assumptions": list(dict.fromkeys(self.assumptions)),
                "limitations": list(dict.fromkeys(self.limitations)), "confidence": confidence, "trace": self.trace,
                "reasoning": self.steps, "news": self.news_cited, **self.extra, **extra}


# ----- intent handlers ------------------------------------------------------------------
def _universe_rank(rs: Reasoner, t: str) -> tuple[tuple[int, int], dict]:
    res = score_universe(rs.profile, rs.provider, rs.window)
    row = next(r for r in res["results"] if r["ticker"] == t)
    st = row["rank_stability"]
    row["stability_text"] = (f"{st['p_top5']:.0%} of {res['sensitivity']['draws']} weight variations "
                             f"(median rank {st['median_rank']}, 80% range {st['rank_p10']}-{st['rank_p90']})")
    return (row["rank"], len(res["results"])), row


def _tension(rs: Reasoner, t: str, fit: dict, news: list[dict]) -> list[str]:
    a = rs.provider.assets[t]
    out = []
    n30 = rs.provider.feature(t, "news_count_30d") if rs.ctx.has_features else None
    sent = rs.provider.feature(t, "news_sentiment_30d") if rs.ctx.has_features else None
    if fit["score"] < 45 and ("high_profile" in a["tags"] or (n30 or 0) >= 8):
        bit = f"{a['name']} is widely covered"
        if n30:
            bit += f" ({n30} articles in 30 days" + (f", sentiment {sent:+.2f}" if sent is not None else "") + ")"
        out.append(bit + ", but attention is not evidence of suitability: the fit is driven by its risk numbers.")
    if rs.inv["horizon_beyond_data"]:
        out.append(f"Your {rs.inv['horizon_years']}-year horizon is longer than the 5 years of data, so the risk figures "
                   "describe one market cycle at most.")
    return out


def _confidence(rs: Reasoner, fits: list[dict]) -> dict:
    order = {"low": 0, "medium": 1, "high": 2}
    if not fits:
        return {"level": "low" if rs.provider.is_placeholder else "medium", "reasons": []}
    worst = min((f["confidence"] for f in fits), key=lambda c: order[c["level"]])
    return {"level": worst["level"], "reasons": list(dict.fromkeys(r for f in fits for r in f["confidence"]["reasons"]))}


def handle_allocation(rs: Reasoner, t: str, weight: float) -> dict:
    a = rs.provider.assets[t]
    rs.step("Understand the question", f"Request to add {weight:.0%} of {a['name']} ({t}) to the fictional portfolio.",
            ["Allocation requests always go through the governance check before any answer."], tool="parse_request")
    rs.profile_step()
    rs.market_step([t])
    end = rs.as_of
    news = rs.news_step([t], (date.fromisoformat(end) - timedelta(days=30)).isoformat(), end, "the last 30 days")
    rank, _ = _universe_rank(rs, t)
    fit = rs.fit_step(t, rank)
    res = check_proposal(rs.profile, {"type": "add", "ticker": t, "weight": weight}, rs.provider, rs.window)
    new_flags = [f for f in res["flags"] if f["new"] and f["severity"] in ("conflict", "warning")]
    grefs = []
    gdetails = []
    for f in new_flags:
        refs = [rs.ev.add_item(e) for e in f["evidence"]]
        grefs += refs
        gdetails.append(f"{f['severity'].upper()}: {f['message']} {cite(*refs)}")
    b, af = res["before"], res["after"]
    vref = None
    if b and af["volatility"] is not None and b["volatility"] is not None:
        vref = rs.ev.add(f"Estimated portfolio volatility after adding {weight:.0%} {t}", af["volatility"],
                         "computed:governance.check_proposal", res["period"], "fraction")
        gdetails.append(f"Portfolio volatility {pct(b['volatility'], 1)} before, {pct(af['volatility'], 1)} after {cite(vref)}.")
    alt = res.get("alternatives") or {}
    rs.tool("check_constraints", {"ticker": t, "weight": weight}, {"status": res["status"], "new_flags": [f["code"] for f in new_flags],
                                                                  "max_weight": alt.get("max_weight_without_new_conflicts")})
    rs.step("Run the governance check", f"Status: {res['status']} ({len(new_flags)} new or worse flag(s)).", gdetails, grefs + [vref],
            tool="check_constraints")
    tension = _tension(rs, t, fit, news)
    challenge = list(tension)
    if res["status"] != "aligned":
        mw = alt.get("max_weight_without_new_conflicts", 0)
        challenge.append(f"Largest {t} position without a new hard conflict: {mw:.1%}." if mw > 0 else
                         f"Any {t} position adds a new conflict for this profile.")
        if alt.get("related_funds"):
            challenge.append("Diversified funds sharing the theme: " + ", ".join(
                f"{x['ticker']} (fit {x['fit_score']})" for x in alt["related_funds"]) + ".")
    rs.step("Challenge the request", "Checked for tensions between the request and the profile, and for alternatives.",
            challenge or ["No tension found."])
    opener = {"conflict": f"A {weight:.0%} position in {a['name']} conflicts with your profile.",
              "review": f"A {weight:.0%} position in {a['name']} is possible but in tension with your profile.",
              "aligned": f"A {weight:.0%} position in {a['name']} is consistent with your profile's constraints."}[res["status"]]
    lines = [opener + f" Its fit is {fit['score']}/100 ({fit['verdict']['label'].lower()}, rank {rank[0]} of {rank[1]})."]
    for f in new_flags[:3]:
        refs = [rs.ev.add_item(e) for e in f["evidence"]]
        lines.append(f"- {f['message']} {cite(*refs)}")
    down = fit["drivers"]["down"][:1]
    if down:
        lines.append(f"- Main drag on the fit: {down[0]['label'].lower()}. {down[0]['explanation']}")
    if vref:
        lines.append(f"- Estimated portfolio volatility would go from {pct(b['volatility'], 1)} to {pct(af['volatility'], 1)} {cite(vref)}.")
    if news:
        s = news[0]
        lines.append(f"- Recent coverage: \"{s['title']}\" ({s.get('publisher')}, {s['published_at'][:10]}) "
                     f"{cite(rs.cite_signal(s))}. News explains attention; it does not change the risk figures.")
    lines += [f"- {x}" for x in challenge if x not in tension][:2]
    lines += [f"- {x}" for x in tension][:1]
    lines.append("This is an educational simulation, not a recommendation.")
    rs.limitations += res["limitations"]
    rs.assumptions.append("The new position is funded by scaling existing holdings down proportionally.")
    return rs.finish("allocation", "\n".join(lines), _confidence(rs, [fit]), governance=res, asset=fit)


def handle_asset(rs: Reasoner, t: str, focus: str = "fit") -> dict:
    a = rs.provider.assets[t]
    rs.step("Understand the question", f"Question about {a['name']} ({t}): {'why it ranks where it does' if focus == 'rank' else 'how it fits this profile'}.",
            tool="parse_request")
    rs.profile_step()
    rs.market_step([t])
    end = rs.as_of
    news = rs.news_step([t], (date.fromisoformat(end) - timedelta(days=60)).isoformat(), end, "the last 60 days")
    rank, row = _universe_rank(rs, t)
    fit = rs.fit_step(t, rank)
    tension = _tension(rs, t, fit, news)
    rs.step("Challenge the result", "Checked for tensions, gates, rank stability and missing data.",
            tension + fit["gates"] + [f"Rank stability: in the top five in {row['stability_text']}.",
                                      f"Confidence: {fit['confidence']['level']}."])
    comps = [c for c in fit["components"] if c["applicable"]]
    lines = [f"{a['name']} ({t}) scores {fit['score']}/100 for your profile: {fit['verdict']['label'].lower()}, "
             f"rank {rank[0]} of {rank[1]}. It is in the top five in {row['stability_text']}."]
    for d in fit["drivers"]["up"][:2]:
        c = next(c for c in comps if c["key"] == d["key"])
        refs = [rs.ev.add_item(e) for e in c["evidence"][:2]]
        lines.append(f"- Helps: {c['label'].lower()} ({c['score']:.0f}/100, weight {c['weight']:.0%}). {c['explanation']} {cite(*refs)}")
    for d in fit["drivers"]["down"][:2]:
        c = next(c for c in comps if c["key"] == d["key"])
        refs = [rs.ev.add_item(e) for e in c["evidence"][:2]]
        lines.append(f"- Hurts: {c['label'].lower()} ({c['score']:.0f}/100, weight {c['weight']:.0%}). {c['explanation']} {cite(*refs)}")
    for g in fit["gates"]:
        lines.append(f"- {g}")
    if news:
        s = news[0]
        lines.append(f"- Latest relevant item: \"{s['title']}\" ({s.get('publisher')}, {s['published_at'][:10]}) {cite(rs.cite_signal(s))}.")
    lines += [f"- {x}" for x in tension[:1]]
    lines.append("Fit describes how the asset matches your stated profile over the data window; it does not predict returns.")
    return rs.finish("asset", "\n".join(lines), _confidence(rs, [fit]), asset=fit)


def handle_compare(rs: Reasoner, tickers: list[str]) -> dict:
    rs.step("Understand the question", f"Compare {', '.join(tickers)} for this profile.", tool="parse_request")
    rs.profile_step()
    rs.market_step(tickers)
    end = rs.as_of
    rs.news_step(tickers, (date.fromisoformat(end) - timedelta(days=30)).isoformat(), end, "the last 30 days", max_items=6)
    res = score_universe(rs.profile, rs.provider, rs.window)
    rows = {r["ticker"]: r for r in res["results"] if r["ticker"] in tickers}
    fits = [rs.fit_step(t, (rows[t]["rank"], len(res["results"]))) for t in tickers]
    fits.sort(key=lambda r: -r["score"])
    best, other = fits[0], fits[-1]
    diffs = []
    bc = {c["key"]: c for c in best["components"]}
    for c in other["components"]:
        b = bc[c["key"]]
        if b["applicable"] and c["applicable"]:
            diffs.append((b["score"] * b["weight"] - c["score"] * c["weight"], b, c))
    diffs.sort(key=lambda x: -abs(x[0]))
    rs.step("Compare the components", f"Largest differences between {best['ticker']} and {other['ticker']}.",
            [f"{b['label']}: {best['ticker']} {b['score']:.0f} vs {other['ticker']} {c['score']:.0f} "
             f"({d:+.1f} weighted points)" for d, b, c in diffs[:4]])
    lines = [f"For your profile, {best['ticker']} fits better ({best['score']}/100, {best['verdict']['label'].lower()}) than "
             + ", ".join(f"{f['ticker']} ({f['score']}/100, {f['verdict']['label'].lower()})" for f in fits[1:]) + "."]
    for f in fits:
        kf = f["key_features"]
        refs = []
        for k in ("vol_1y", "mdd_3y", "sharpe_3y", "news_sentiment_30d"):
            if kf.get(k) is not None:
                refs.append(rs.ev.add(f"{f['ticker']}: {rs.provider.feature_definitions().get(k, {}).get('label', k)}", kf[k],
                                      rs.provider.feature_ref(f["ticker"], k), rs.ctx.feature_period,
                                      rs.provider.feature_definitions().get(k, {}).get("unit")))
        sharpe = "n/a" if kf.get("sharpe_3y") is None else f"{kf['sharpe_3y']:.2f}"
        lines.append(f"- {f['ticker']}: volatility {pct(kf.get('vol_1y'))}, worst fall (3y) {pct(kf.get('mdd_3y'))}, "
                     f"Sharpe (3y) {sharpe}"
                     + (f", news sentiment {kf['news_sentiment_30d']:+.2f}" if kf.get("news_sentiment_30d") is not None else "")
                     + f" {cite(*refs)}.")
    for d, b, c in diffs[:2]:
        lower, low_c = (other["ticker"], c) if b["score"] >= c["score"] else (best["ticker"], b)
        lines.append(f"- {b['label']}: {best['ticker']} {b['score']:.0f} vs {other['ticker']} {c['score']:.0f} "
                     f"({abs(d):.1f} weighted points in favour of {best['ticker'] if d > 0 else other['ticker']}). "
                     f"Why {lower} is lower: {low_c['explanation']}")
    lines.append("Higher fit means closer to your stated profile, not a higher expected return.")
    return rs.finish("compare", "\n".join(lines), _confidence(rs, fits), comparison=[{k: f[k] for k in ("ticker", "score", "verdict", "status")} for f in fits])


def handle_ranking(rs: Reasoner, n: int = 5) -> dict:
    rs.step("Understand the question", f"Which assets fit this profile best (top {n}).", tool="parse_request")
    rs.profile_step()
    res = score_universe(rs.profile, rs.provider, rs.window)
    rs.tool("score_assets", {"top_n": n}, [{"ticker": r["ticker"], "score": r["score"]} for r in res["results"][:n]])
    eligible = [r for r in res["results"] if r["status"] not in ("excluded",) and r["verdict"]["key"] != "outside_profile"]
    top = eligible[:n]
    details = [f"{r['rank']}. {r['ticker']} {r['score']}/100 ({r['verdict']['label'].lower()}), top five in "
               f"{r['rank_stability']['p_top5']:.0%} of weight variations" for r in top]
    excluded = [r["ticker"] for r in res["results"] if r["status"] == "excluded"]
    outside = [r["ticker"] for r in res["results"] if r["verdict"]["key"] == "outside_profile"]
    if excluded:
        details.append("Excluded by your preferences: " + ", ".join(excluded) + ".")
    if outside:
        details.append("Outside your risk profile (score capped at 45): " + ", ".join(outside) + ".")
    rs.step("Rank the universe", f"Scored {len(res['results'])} assets; {len(eligible)} are eligible.", details, tool="score_assets")
    regime = rs.provider.market_regime()
    if regime:
        r_ref = rs.ev.add("Market regime", regime["label"], "asset_features.json#market_regime", regime["as_of"], "label")
        rs.step("Check market conditions", regime["summary"] + f" {cite(r_ref)}", [regime["note"]], [r_ref], tool="get_market_regime")
    lines = [f"Best fits for your profile ({rs.inv['risk_label'].lower()} risk, {rs.inv['horizon_years']}-year horizon), out of "
             f"{len(res['results'])} assets:"]
    groups = set()
    for r in top:
        up = r["drivers"]["up"][:1]
        c = next((c for c in r["components"] if up and c["key"] == up[0]["key"]), None)
        refs = [rs.ev.add_item(e) for e in (c["evidence"][:1] if c else [])]
        groups.add(r["asset_group"])
        why = f" Strongest point: {c['label'].lower()} ({c['score']:.0f}/100)." if c else ""
        hold = f" You already hold {pct(r['current_weight'])}." if r["current_weight"] > 0 else ""
        lines.append(f"{r['rank']}. {r['ticker']} ({r['name']}): {r['score']}/100, {r['verdict']['label'].lower()}; "
                     f"top five in {r['rank_stability']['p_top5']:.0%} of weight variations.{why}{hold} {cite(*refs)}")
    if len(groups) == 1:
        lines.append(f"All five are {list(groups)[0].replace('_', ' ')} assets: combine with other asset types for diversification.")
    port = rs.inv.get("portfolio") or {}
    if port.get("tech_growth_share", 0) >= 0.5:
        lines.append(f"Your current holdings are {pct(port['tech_growth_share'])} tech or growth assets, so assets that move "
                     "differently score high on diversification, and more tech is marked as added concentration.")
    if excluded:
        lines.append(f"Excluded by your preferences: {', '.join(excluded)}.")
    lines.append("A ranking describes fit with your stated profile; it is not a recommendation to buy.")
    rs.assumptions += res["assumptions"]
    return rs.finish("ranking", "\n".join(lines), _confidence(rs, top), ranking=[{k: r[k] for k in ("ticker", "score", "verdict", "rank", "rank_stability")} for r in top])


def _moves_in_period(rs: Reasoner, t: str, start: str, end: str, n: int = 4) -> tuple[list[dict], dict | None]:
    """Largest daily moves and the period return, computed from the price file (listing currency)."""
    doc = rs.provider.price_history(t) or {}
    pts = [p for p in doc.get("points") or [] if p.get("adj_close")]
    if len(pts) < 30:
        return [], None
    rets = [(pts[i]["date"], pts[i]["adj_close"] / pts[i - 1]["adj_close"] - 1) for i in range(1, len(pts))]
    idx = [i for i, (d, _) in enumerate(rets) if start <= d <= end]
    if not idx:
        return [], None
    out = []
    for i in idx:
        trail = [r for _, r in rets[max(0, i - 252):i]]
        if len(trail) < 40:
            continue
        mean = sum(trail) / len(trail)
        sd = (sum((r - mean) ** 2 for r in trail) / len(trail)) ** 0.5
        if sd > 0:
            out.append({"date": rets[i][0], "return": round(rets[i][1], 4), "z": round(rets[i][1] / sd, 2)})
    out.sort(key=lambda m: -abs(m["z"]))
    chosen = []
    for m in out:
        if all(abs((date.fromisoformat(m["date"]) - date.fromisoformat(c["date"])).days) > 3 for c in chosen):
            chosen.append(m)
        if len(chosen) == n:
            break
    first, last = pts[max(0, idx[0])], pts[idx[-1] + 1]
    period_ret = {"start": first["date"], "end": last["date"], "return": round(last["adj_close"] / first["adj_close"] - 1, 4),
                  "currency": doc.get("currency")}
    return sorted(chosen, key=lambda m: m["date"]), period_ret


def _policy_summary(rs: Reasoner, start: str, end: str) -> list[tuple[str, list[int]]]:
    """One sentence per central bank: how many decisions in the window and the rate path."""
    out = []
    macro = [s for s in rs.provider.macro_signals(start, end) if s.get("kind") == "macro"]
    for sid, who in (("V39079", "The Bank of Canada"), ("DFF", "The Federal Reserve")):
        items = sorted((s for s in macro if s.get("series_id") == sid), key=lambda s: s["published_at"])
        if not items:
            continue
        ups = sum(1 for s in items if s["value_after"] > s["value_before"])
        downs = len(items) - ups
        refs = [rs.cite_signal(items[0]), rs.cite_signal(items[-1])]
        path = f"({items[0]['value_before']:.2f}% to {items[-1]['value_after']:.2f}%)"
        if len(items) == 1:
            verb = "raised" if ups else "cut"
            out.append((f"{who} {verb} its policy rate on {items[0]['published_at'][:10]} {path}", refs[:1]))
            continue
        verb = "raised rates" if ups and not downs else "cut rates" if downs and not ups else "changed rates"
        out.append((f"{who} {verb} {len(items)} times between {items[0]['published_at'][:10]} and "
                    f"{items[-1]['published_at'][:10]} {path}", refs))
    return out


def handle_explain_move(rs: Reasoner, t: str, period: tuple[str, str, str] | None) -> dict:
    a = rs.provider.assets[t]
    rs.step("Understand the question", f"Explain a price move in {a['name']} ({t})" + (f" during {period[2]}." if period else "."),
            ["Moves are found in the price data first; news is then looked up around those dates."], tool="parse_request")
    det = rs.provider.feature_details(t) if rs.ctx.has_features else {}
    ql = rs.q.lower()
    direction = -1 if any(w in ql for w in ("fall", "fell", "drop", "decline", "plunge", "crash", "sink", "sank", "tumble", "lose", "lost")) \
        else 1 if any(w in ql for w in ("rise", "rose", "jump", "surge", "rally", "soar", "gain", "spike")) else 0
    dd = det.get("drawdown_5y")
    if not period and direction < 0 and dd:
        # "Why did X drop?" with no date: explain its worst fall, from peak to trough.
        period = (dd["peak_date"], dd["trough_date"], f"its worst fall ({dd['peak_date']} to {dd['trough_date']})")
    if period:
        in_p, pr = _moves_in_period(rs, t, period[0], period[1], n=8)
    else:
        in_p, pr = sorted(det.get("big_moves") or [], key=lambda m: -abs(m["z"])), None
    if direction:
        in_p = [m for m in in_p if m["return"] * direction > 0] or in_p
    # Ordinary days (under 1.5 times the usual move) are not worth explaining.
    in_p = [m for m in in_p if abs(m["z"]) >= 1.5] or in_p[:1]
    in_p = sorted(sorted(in_p, key=lambda m: -abs(m["z"]))[:4], key=lambda m: m["date"])
    refs, details = [], []
    pr_ref = None
    if pr:
        pr_ref = rs.ev.add(f"{t}: return {pr['start']} to {pr['end']} ({pr['currency']})", pr["return"], f"prices/{t}.json#adj_close",
                           f"{pr['start']} to {pr['end']}", "fraction")
        details.append(f"Return over {period[2]}: {spct(pr['return'])} in {pr['currency']} ({pr['start']} to {pr['end']}) {cite(pr_ref)}.")
    for m in in_p[:4]:
        r = rs.ev.add(f"{t}: daily move on {m['date']}", m["return"], f"prices/{t}.json#adj_close", m["date"], "fraction")
        refs.append(r)
        details.append(f"{m['date']}: {spct(m['return'], 1)} in one day ({abs(m['z']):.1f} times its usual daily move) {cite(r)}.")
    st, st_ref = rs.feat(t, "stress_2022") if period and period[2] == "2022" else (None, None)
    if st is not None:
        details.append(f"Calendar-2022 return in CAD: {spct(st)} {cite(st_ref)}.")
    dd_line, dd_ref = None, None
    if dd and (not period or period[0] <= dd["trough_date"] <= period[1] or period[0] <= dd["peak_date"] <= period[1]):
        mdd, dd_ref = rs.feat(t, "mdd_5y")
        if mdd is not None:
            dd_line = (f"Its worst fall in the data, {pct(mdd)}, ran from {dd['peak_date']} to {dd['trough_date']} " +
                       (f"and took {dd['recovered_after_days']} trading days to recover" if dd.get("recovered_after_days") else
                        "and had not been recovered by the end of the data"))
            details.append(dd_line + f" {cite(dd_ref)}.")
    rs.tool("get_price_moves", {"ticker": t, "period": period[:2] if period else None}, {"moves": in_p[:4], "drawdown": dd})
    rs.step("Find the moves in the price data",
            (f"Return over the period and its {len(in_p)} largest daily moves (measured against the trailing year's usual move)."
             if period else f"The {len(in_p)} largest unusual daily moves in the data (3+ times the usual size)."),
            details or ["No price data for this period."], refs + [pr_ref, st_ref, dd_ref], tool="get_price_moves")
    linked = []
    rank = {"high": 0, "medium": 1, "low": 2}
    for m in in_p[:4]:
        d = date.fromisoformat(m["date"])
        win = [s for s in rs.provider.signals_between([t], (d - timedelta(days=2)).isoformat(), (d + timedelta(days=1)).isoformat())
               if s.get("kind") in ("news", "filing")]
        # News before filings, matched-to-this-move first, then materiality, then closeness to the move date.
        win.sort(key=lambda s: (s.get("kind") != "news", (s.get("linked_move") or {}).get("date") != m["date"],
                                rank.get(s.get("materiality"), 3),
                                abs((date.fromisoformat(s["published_at"][:10]) - d).days)))
        linked.append((m, win[:3]))
    ndetails, nrefs = [], []
    for m, win in linked:
        if not win:
            ndetails.append(f"{m['date']}: no filing or allow-listed article found from 2 days before to 1 day after.")
        for s in win:
            r = rs.cite_signal(s)
            nrefs.append(r)
            ndetails.append(f"{m['date']} move: {s['published_at'][:10]} · {s.get('publisher')}: \"{s['title']}\" {cite(r)}")
    policy = _policy_summary(rs, period[0], period[1]) if period else []
    if not period and in_p:
        for m in in_p[:3]:
            d = date.fromisoformat(m["date"])
            for s in rs.provider.macro_signals((d - timedelta(days=3)).isoformat(), (d + timedelta(days=1)).isoformat()):
                if s.get("kind") == "macro":
                    policy.append((f"{s['published_at'][:10]}: {s['title']}", [rs.cite_signal(s)]))
    for text, prefs in policy:
        nrefs += prefs
        ndetails.append(f"Policy: {text} {cite(*prefs)}.")
    rs.tool("get_news_around_moves", {"ticker": t, "moves": [m["date"] for m, _ in linked]},
            {"items": sum(len(w) for _, w in linked), "policy_context": len(policy)})
    rs.step("Match news to the move dates", f"Looked up filings and allow-listed articles from 2 days before to 1 day after each move"
                                           + (", and policy-rate decisions in the period." if period else "."), ndetails, nrefs,
            tool="get_news_around_moves")
    rs.step("Challenge the explanation", "News published near a move is context, not proof of cause.",
            ["Several events can coincide; market-wide moves (rates, the index) also matter.",
             "Coverage comes from the allow-listed outlets and SEC filings only; a day without a matching item is reported as such."])
    lines = []
    if pr:
        lines.append(f"Over {period[2]}, {a['name']} returned {spct(pr['return'])} in {pr['currency']} {cite(pr_ref)}"
                     + (f" ({spct(st)} in CAD {cite(st_ref)})" if st is not None and pr["currency"] != "CAD" else "") + ".")
    if dd_line:
        lines.append(dd_line + f" {cite(dd_ref)}.")
    if in_p:
        lines.append(f"The largest daily moves{' in ' + period[2] if period else ' in the data'}, with what was published around them:")
    else:
        lines.append(f"No unusually large daily move in {a['name']}{' during ' + period[2] if period else ''} in the price data.")
    for m, win in linked[:4]:
        r = refs[in_p.index(m)] if in_p.index(m) < len(refs) else None
        if win:
            s = win[0]
            lines.append(f"- {m['date']} ({spct(m['return'], 1)} {cite(r)}): \"{s['title']}\" ({s.get('publisher')}, "
                         f"{s['published_at'][:10]}) {cite(rs.cite_signal(s))}")
        else:
            lines.append(f"- {m['date']} ({spct(m['return'], 1)} {cite(r)}): no matching filing or article in the sources searched.")
    for text, prefs in policy[:2]:
        lines.append(f"- Rates: {text} {cite(*prefs)}.")
    lines.append("These items were published around the moves; they are context, not proof of what caused them.")
    conf = {"level": "medium" if any(w for _, w in linked) else "low",
            "reasons": ["Explanations link dates; they cannot establish cause."] +
                       ([] if any(w for _, w in linked) else ["No news found around the moves."])}
    return rs.finish("explain_move", "\n".join(lines), conf)


def handle_news(rs: Reasoner, tickers: list[str], period: tuple[str, str, str] | None) -> dict:
    scope = tickers or [h["ticker"] for h in rs.profile["holdings"]]
    label = "your holdings" if not tickers else ", ".join(tickers)
    rs.step("Understand the question", f"Recent news and filings for {label}.", tool="parse_request")
    end = period[1] if period else rs.as_of
    start = period[0] if period else (date.fromisoformat(end) - timedelta(days=30)).isoformat()
    lab = period[2] if period else "the last 30 days"
    picked = rs.news_step(scope, start, end, lab, max_items=8, include_macro=not tickers)
    rs.profile_step()
    effects, refs = [], []
    for t in scope[:4]:
        r = score_asset(t, rs.ctx)
        c = next(c for c in r["components"] if c["key"] == "news_events")
        for e in c["evidence"][:1]:
            refs.append(rs.ev.add_item(e))
        effects.append(f"{t}: news component {c['score']:.0f}/100 x {c['weight']:.0%} weight ({c['contribution']:.1f} of {r['score']} pts). "
                       f"{c['explanation']}")
        if rs.inv["needs"]["esg"] != "none":
            lvl = rs.provider.feature(t, "esg_controversy_level")
            if lvl and lvl != "none_found":
                effects.append(f"{t}: ESG controversy evidence is {lvl} (counted in preference fit).")
    rs.tool("score_asset", {"tickers": scope[:4], "component": "news_events"}, {"assets": len(scope[:4])})
    rs.step("Measure the effect on fit", "Read the news component of each asset's fit score.", effects, refs, tool="score_asset")
    lines = []
    if picked:
        lines.append(f"Main items for {label} over {lab}:")
        for s in picked[:6]:
            lines.append(f"- {s['published_at'][:10]} · {s.get('publisher')}: \"{s['title']}\" "
                         f"({s.get('event_type', 'general').replace('_', ' ')}) {cite(rs.cite_signal(s))}")
    else:
        lines.append(f"No filings or allow-listed articles found for {label} over {lab}. That is not evidence that nothing happened.")
    lines += [f"- {e}" for e in effects[:3]]
    lines.append("Headlines feed the explanation and a small part of the fit score (the news component); they do not predict returns.")
    return rs.finish("news", "\n".join(lines), {"level": "medium" if picked else "low",
                                                 "reasons": ["Coverage is limited to SEC filings, central-bank releases and the allow-listed outlets."]})


def handle_market(rs: Reasoner) -> dict:
    rs.step("Understand the question", "Current market and rate conditions, and what they mean for this profile.", tool="parse_request")
    regime = rs.provider.market_regime()
    lines = []
    if not regime:
        rs.limitations.append("Market regime needs asset_features.json.")
        return rs.finish("market", "Market regime data is not loaded.", {"level": "low", "reasons": ["No features loaded."]})
    refs, details = [], []
    r0 = rs.ev.add("Market regime", regime["label"], "asset_features.json#market_regime", regime["as_of"], "label")
    for t, vals in regime["equity"].items():
        for k in ("pct_from_52w_high", "px_vs_sma_200", "p_turbulent"):
            if vals.get(k) is not None:
                refs.append(rs.ev.add(f"{t}: {rs.provider.feature_definitions().get(k, {}).get('label', k)}", vals[k],
                                      rs.provider.feature_ref(t, k), regime["as_of"], "fraction"))
        hi = vals.get("pct_from_52w_high")
        where = "at its 52-week high" if hi is not None and abs(hi) < 0.005 else f"{pct(hi)} from its 52-week high"
        details.append(f"{t}: {where}, {spct(vals.get('px_vs_sma_200'))} vs its "
                       f"200-day average, turbulent-regime probability {pct(vals.get('p_turbulent'))} {cite(*refs[-3:])}.")
    rates = regime.get("rates") or {}
    for key, label in (("ca_policy_rate", "Bank of Canada policy rate"), ("us_fed_funds", "U.S. fed funds rate"),
                       ("ca_10y", "Canada 10-year yield"), ("us_10y", "U.S. 10-year yield")):
        if key in rates:
            v = rates[key]
            refs.append(rs.ev.add(label, v["value"], f"macro_series.json#{v['series_id']}", v["date"], "percent"))
            details.append(f"{label}: {v['value']:.2f}% on {v['date']} {cite(refs[-1])}.")
    if "ca_policy_direction" in rates:
        details.append(f"Bank of Canada over the last 6 months: {rates['ca_policy_direction']} ({rates['ca_policy_change_6m']:+.2f} points).")
    rs.tool("get_market_regime", {}, {"label": regime["label"], "rates": {k: v for k, v in rates.items() if not isinstance(v, dict)}})
    rs.step("Read market conditions", regime["summary"] + f" {cite(r0)}", details + regime["rules"], [r0] + refs, tool="get_market_regime")
    end = rs.as_of
    rs.news_step([], (date.fromisoformat(end) - timedelta(days=45)).isoformat(), end, "the last 45 days", include_macro=True)
    rs.profile_step()
    lines.append(regime["summary"] + f" {cite(r0)}")
    lines += [f"- {d}" for d in details[:4]]
    if rs.inv["objective"] == "preservation" and regime["label"] in ("volatile", "correction", "stress"):
        lines.append("- For a capital-preservation profile, a turbulent market makes the loss tolerance the binding constraint.")
    elif rs.inv["objective"] == "growth":
        lines.append("- For a long-horizon growth profile, current conditions matter less than diversification and position size.")
    lines.append(regime["note"])
    return rs.finish("market", "\n".join(lines), {"level": "medium", "reasons": ["A regime label is a description, not a forecast."]})


def handle_portfolio(rs: Reasoner) -> dict:
    rs.step("Understand the question", "Review the portfolio against the target for this profile.", tool="parse_request")
    rs.profile_step()
    s = build_suggestions(rs.profile, rs.provider, rs.window)
    rs.tool("get_suggestions", {}, {"count": len(s["items"])})
    items = s["items"][:3]
    refs = [rs.ev.add_item(e) for it in items for e in it["evidence"][:2]]
    rs.step("Compare portfolio and target", f"{len(s['items'])} gap(s) above the thresholds.",
            [f"{it['severity']}: {it['title']}. {it['detail']}" for it in items], refs, tool="get_suggestions")
    if not items:
        answer = "No gap above the thresholds was found between your portfolio and the target for your profile."
    else:
        parts = []
        for it in items:
            c = it["candidates"][:2]
            cand = (" Candidates: " + ", ".join(
                f"{x['ticker']} (fit {x['fit_score']}" + (f", {x['impact']['axis']} {x['impact']['from']:.0f} -> {x['impact']['to']:.0f}"
                                                        if x.get("impact") and x["impact"]["from"] is not None and x["impact"]["to"] is not None else "") + ")"
                for x in c)) if c else ""
            ev_refs = [rs.ev.add_item(e) for e in it["evidence"][:2]]
            parts.append(f"- {it['title']}. {it['detail']}{cand + '.' if cand else ''} {cite(*ev_refs)}")
        answer = "Main gaps between your portfolio and the target for your profile:\n" + "\n".join(parts)
    rs.assumptions += s["notes"]
    return rs.finish("portfolio", answer + "\nThis is an educational simulation, not a recommendation.",
                     {"level": "low" if rs.provider.is_placeholder else "medium",
                      "reasons": ["Metrics are placeholder values."] if rs.provider.is_placeholder else []})


def handle_stress(rs: Reasoner) -> dict:
    rs.step("Understand the question", "Replay the 2022 rate shock on the fictional portfolio.", tool="parse_request")
    w, note = rs.provider.resolve_window(rs.window)
    res = stress_test(weights_from_holdings(rs.profile["holdings"]), rs.provider, w, "2022_rates_shock")
    rs.tool("run_stress", {"scenario": "2022_rates_shock"}, {"return": res.get("portfolio_return_on_covered"), "coverage": res.get("coverage")})
    refs = [rs.ev.add(f"{l['ticker']} 2022 return", l["scenario_return"], l["source_id"], res.get("period"), "fraction")
            for l in res.get("lines", [])]
    rs.step("Replay the scenario", res.get("method", ""), [f"{l['ticker']}: weight {pct(l['weight'])}, 2022 return {spct(l['scenario_return'])} "
                                                         f"{cite(r)}" for l, r in zip(res.get("lines", []), refs)], refs, tool="run_stress")
    macro = rs.provider.macro_signals("2022-01-01", "2022-12-31")
    mrefs = [rs.cite_signal(s) for s in macro if s.get("kind") == "macro"][:6]
    if mrefs:
        rs.step("Add the policy context", f"{len(mrefs)} policy-rate decisions in 2022 from the macro series.",
                [f"{s['published_at'][:10]}: {s['title']}" for s in macro if s.get("kind") == "macro"][:6], mrefs)
    if not res.get("available"):
        answer = "The 2022 scenario cannot be computed: no scenario data for your holdings in the loaded metrics."
    else:
        answer = (f"Replaying {res['label']} ({res['period']}): the covered part of your portfolio ({res['coverage']:.0%} of holdings) "
                  f"would have returned {res['portfolio_return_on_covered']:+.1%} in CAD {cite(*refs[:3])}.")
        if res["missing"]:
            answer += f" No scenario data for: {', '.join(res['missing'])}."
        if mrefs:
            answer += f" Central banks raised rates repeatedly that year {cite(*mrefs[:3])}, which is why bonds fell with stocks."
        answer += " A past scenario shows one possible path, not a forecast."
    if note:
        rs.limitations.append(note)
    rs.assumptions.append(res.get("method", ""))
    return rs.finish("stress", answer, {"level": "low" if rs.provider.is_placeholder or res.get("missing") else "medium",
                                         "reasons": ([f"Coverage {res['coverage']:.0%}."] if res.get("missing") else [])}, stress=res)


def answer(question: str, profile: dict, provider: DataProvider, window: str | None = None) -> dict:
    rs = Reasoner(question, profile, provider, window)
    tickers = find_tickers(question, provider)
    pm = PCT_RE.search(question)
    intent = classify_intent(question, tickers, bool(pm))
    period = parse_period(question, rs.as_of)
    rs.tool("parse_request", {"question": question},
            {"intent": intent, "tickers": tickers, "percentage": pm.group(1) if pm else None, "period": period})
    rs.extra["parsed"] = {"intent": intent, "intent_label": INTENT_LABELS[intent], "tickers": tickers,
                          "percentage": pm.group(1) if pm else None, "period": period}
    q = question.lower()
    if intent == "allocation":
        return handle_allocation(rs, tickers[0], float(pm.group(1).replace(",", ".")) / 100)
    if intent == "explain_move":
        return handle_explain_move(rs, tickers[0], period)
    if intent == "news":
        return handle_news(rs, tickers, period)
    if intent == "compare":
        return handle_compare(rs, tickers[:3])
    if intent == "asset":
        return handle_asset(rs, tickers[0], "rank" if any(w in q for w in ("rank", "score", "why is", "why does")) else "fit")
    if intent == "ranking":
        m = re.search(r"\btop\s+(\d{1,2})\b", q)
        return handle_ranking(rs, min(10, int(m.group(1))) if m else 5)
    if intent == "stress":
        return handle_stress(rs)
    if intent == "market":
        return handle_market(rs)
    if intent == "portfolio":
        return handle_portfolio(rs)
    rs.step("Understand the question", "The question did not match a known type.", tool="parse_request")
    return rs.finish("help",
                     "I can answer: (1) allocation requests (\"Should I put 30% in Tesla?\"), (2) how an asset fits you or why it "
                     "ranks where it does (\"Why is NVDA ranked low?\"), (3) comparisons (\"Compare SPY and ESGV\"), (4) which "
                     "assets fit you best, (5) news on an asset or your holdings, (6) why a price moved (\"Why did Shopify fall in "
                     "2022?\"), (7) market and rate conditions, (8) portfolio diversification and (9) the 2022 stress replay.",
                     {"level": "n/a", "reasons": ["Question not recognised by the local planner."]})
