"""Rule-based suggestions from radar gaps and governance flags, plus top picks.

Rules produce structured suggestions with numbers and a what-if impact. An LLM
may rephrase them, but the content (which assets, which numbers) comes from here.
"""

from __future__ import annotations

from .data.provider import DataProvider
from .governance import evaluate_weights
from .portfolio import add_position, weights_from_holdings
from .profiles import profile_drawdown_tolerance
from .radar import axis_values, compute_radar
from .scoring import score_universe

WHAT_IF_WEIGHT = 0.10
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _candidate(r: dict, reason: str, impact: dict | None = None) -> dict:
    return {"ticker": r["ticker"], "name": r["name"], "fit_score": r["score"], "reason": reason, "impact": impact}


class _Builder:
    def __init__(self, profile: dict, provider: DataProvider, window: str, scores: dict, radar: dict):
        self.profile, self.provider, self.window = profile, provider, window
        self.weights = weights_from_holdings(profile["holdings"])
        self.scored = {r["ticker"]: r for r in scores["results"]}
        self.axes = {a["key"]: a for a in radar["axes"]}
        self.items: list[dict] = []
        self.used: set[str] = set()

    def eligible(self, predicate=lambda a: True, exclude_held: bool = True) -> list[dict]:
        out = []
        for t, r in self.scored.items():
            if r["status"] != "ok":
                continue
            if exclude_held and t in self.weights:
                continue
            if predicate(self.provider.assets[t]):
                out.append(r)
        return sorted(out, key=lambda r: -r["score"])

    def what_if(self, ticker: str, axis: str) -> dict:
        before = self.axes[axis]["value"]
        values, _ = axis_values(add_position(self.weights, ticker, WHAT_IF_WEIGHT), self.profile, self.provider, self.window)
        after = values[axis]["value"]
        return {"axis": axis, "from": before, "to": None if after is None else round(after, 1),
                "assumption": f"adds {WHAT_IF_WEIGHT:.0%} of {ticker} and scales other holdings down proportionally"}

    def candidates(self, axis: str, pool: list[dict], reason, n: int = 3, key=None) -> list[dict]:
        if key:
            pool = sorted(pool, key=key)
        # Prefer assets not already proposed by an earlier suggestion, so the
        # list shows a range of options instead of the same three tickers.
        pool = sorted(pool, key=lambda r: r["ticker"] in self.used)  # stable: keeps score order within groups
        chosen = pool[:n]
        self.used.update(r["ticker"] for r in chosen)
        return [_candidate(r, reason(r), self.what_if(r["ticker"], axis)) for r in chosen]

    def add(self, sid: str, axis: str | None, severity: str, kind: str, title: str, detail: str,
            candidates: list | None = None, evidence: list | None = None, gap: float = 0.0) -> None:
        self.items.append({"id": sid, "axis": axis, "severity": severity, "kind": kind, "title": title,
                           "detail": detail, "candidates": candidates or [], "evidence": evidence or [], "_gap": abs(gap)})


def build_suggestions(profile: dict, provider: DataProvider, window: str | None = None,
                      scores: dict | None = None, radar: dict | None = None) -> dict:
    window, _ = provider.resolve_window(window)
    scores = scores or score_universe(profile, provider, window)
    radar = radar or compute_radar(profile, provider, window)
    if not radar["has_holdings"]:
        return {"items": [{"id": "add_holdings", "axis": None, "severity": "medium", "kind": "setup",
                           "title": "Add your (fictional) holdings",
                           "detail": "Suggestions compare your portfolio with a target for your profile. "
                                     "Enter holdings or load a persona preset to see them.",
                           "candidates": [], "evidence": []}],
                "top_picks": top_picks(scores), "notes": []}

    b = _Builder(profile, provider, window, scores, radar)
    _governance_rules(b)
    _radar_rules(b)
    items = sorted(b.items, key=lambda s: (SEVERITY_ORDER[s["severity"]], -s["_gap"]))
    for s in items:
        s.pop("_gap")
    return {
        "items": items,
        "top_picks": top_picks(scores),
        "notes": [f"What-if impacts assume adding {WHAT_IF_WEIGHT:.0%} of a candidate and scaling other holdings down.",
                  "Candidates are limited to assets whose fit status is OK for this profile."],
    }


def _governance_rules(b: _Builder) -> None:
    flags, _ = evaluate_weights(b.weights, b.profile, b.provider, b.window)
    assets = b.provider.assets
    esg_tickers = {f["subject"] for f in flags if f["code"] == "esg_exclusion"}
    cap_msgs = {f["subject"]: f["message"] for f in flags if f["code"] == "position_cap" and f["severity"] == "conflict"}
    drawdowns = [f for f in flags if f["code"] == "horizon_drawdown"]
    for f in flags:
        if f["code"] == "esg_exclusion":
            t = f["subject"]
            group = assets[t]["asset_group"]
            pool = b.eligible(lambda a, g=group: a["asset_group"] == g or a["asset_group"] == "broad_equity_fund")
            detail = f["message"] + (" " + cap_msgs[t] if t in cap_msgs else "")
            b.add(f"esg_{t}", "esg", "high", "review", f"{t} conflicts with your ESG screen", detail,
                  b.candidates("esg", pool, lambda r: "passes your ESG screen and fits the profile"),
                  f["evidence"], gap=f["value"] * 100)
        elif f["code"] == "position_cap" and f["severity"] == "conflict" and f["subject"] not in esg_tickers:
            t = f["subject"]
            b.add(f"cap_{t}", "diversification", "high", "rebalance", f"{t} is above the single-position limit",
                  f["message"] + " Trimming it toward the limit reduces single-company risk.", gap=f["value"] * 100)
        elif f["code"] == "crypto_cap":
            b.add("crypto_cap", "risk", "high", "rebalance", "Crypto is above the limit for this profile",
                  f["message"] + " Crypto had some of the deepest drawdowns in the data window.", gap=f["value"] * 100)
        elif f["code"] == "sector_concentration":
            b.add(f"sector_{f['subject']}", "diversification", "medium", "rebalance",
                  f"Concentrated in {f['subject']}", f["message"], gap=f["value"] * 100)
        # portfolio_risk is covered by the risk-axis rule (with contributors and candidates);
        # defensive_floor by the defensiveness-axis rule.

    if drawdowns:
        tol = profile_drawdown_tolerance(b.profile)[0]
        implied = sum(f["value"] * abs(b.provider.metric(f["subject"], b.window, "max_drawdown")) for f in drawdowns)
        names = ", ".join(f"{f['subject']} (fell {abs(b.provider.metric(f['subject'], b.window, 'max_drawdown')):.0%}, "
                          f"{f['value']:.0%} weight)" for f in sorted(drawdowns, key=lambda f: -f["value"]))
        b.add("horizon_drawdowns", "risk", "medium" if implied > tol / 2 else "low", "review",
              "Some holdings fell more than your horizon tolerance",
              f"Holdings whose worst fall in {b.provider.window_period(b.window)} exceeded your {tol:.0%} loss tolerance: {names}. "
              f"A repeat of each worst fall would cost about {implied:.1%} of the portfolio in total (falls do not all happen at once).",
              evidence=[e for f in drawdowns for e in f["evidence"]], gap=implied * 100)


def _radar_rules(b: _Builder) -> None:
    ax = b.axes
    assets = b.provider.assets

    def gap(key: str) -> float | None:
        a = ax[key]
        return None if a["value"] is None or a["target"] is None else a["value"] - a["target"]

    g = gap("diversification")
    if g is not None and g < -10:
        pool = b.eligible()
        cands = b.candidates("diversification", pool,
                             lambda r: next(c["explanation"] for c in r["components"] if c["key"] == "diversification"),
                             key=lambda r: -next(c["score"] for c in r["components"] if c["key"] == "diversification"))
        b.add("diversify", "diversification", "high" if g < -25 else "medium", "add",
              "Diversify: your holdings move together or are concentrated",
              f"Diversification is {ax['diversification']['value']:.0f} vs a target of {ax['diversification']['target']:.0f}. "
              + ax["diversification"]["explanation"], cands, ax["diversification"]["evidence"], gap=g)

    g = gap("risk")
    if g is not None and g > 10:
        _, stats = axis_values(b.weights, b.profile, b.provider, b.window)
        rc = sorted(stats.risk_contributions().items(), key=lambda kv: -kv[1])[:3]
        contributors = ", ".join(f"{t} {s:.0%} of risk at {b.weights[t]:.0%} weight" for t, s in rc)
        pool = b.eligible(lambda a: a["is_defensive"])
        b.add("reduce_risk", "risk", "high" if g > 25 else "medium", "rebalance",
              "Portfolio risk is above the target for your profile",
              f"{ax['risk']['explanation']} Largest risk contributors: {contributors}.",
              b.candidates("risk", pool, lambda r: f"defensive; volatility {r['metrics']['ann_vol']:.0%}"),
              ax["risk"]["evidence"], gap=g)
    elif g is not None and g < -15 and b.profile["risk_level"] in ("moderate", "high"):
        pool = b.eligible(lambda a: bool({"growth", "tech"} & set(a["tags"])))
        b.add("risk_headroom", "risk", "low", "add", "Risk is below what your profile allows",
              f"{ax['risk']['explanation']} Lower risk than targeted may also mean lower growth potential.",
              b.candidates("risk", pool, lambda r: "growth exposure that fits your risk band"), ax["risk"]["evidence"], gap=g)

    g = gap("defensiveness")
    if g is not None and g < -10:
        pool = b.eligible(lambda a: a["is_defensive"])
        b.add("defensive", "defensiveness", "medium", "add", "Add a defensive sleeve",
              f"Defensiveness is {ax['defensiveness']['value']:.0f} vs a target of {ax['defensiveness']['target']:.0f}. "
              f"{ax['defensiveness']['explanation']} In 2022 both stocks and bonds fell, so the data shows bonds alone were not a full hedge.",
              b.candidates("defensiveness", pool, lambda r: f"{r['sector']}; worst fall {abs(r['metrics']['max_drawdown'] or 0):.0%}"),
              ax["defensiveness"]["evidence"], gap=g)

    g = gap("growth")
    if g is not None and g > 15:
        pool = b.eligible(lambda a: not ({"growth", "tech"} & set(a["tags"])))
        b.add("growth_tilt", "growth", "medium", "add", "Heavy growth / tech tilt",
              f"{ax['growth']['explanation']} The target for your profile is {ax['growth']['target']:.0f}%. "
              f"Non-tech holdings reduce dependence on one theme.",
              b.candidates("growth", pool, lambda r: f"non-tech: {r['sector']}"), ax["growth"]["evidence"], gap=g)
    elif g is not None and g < -15:
        pool = b.eligible(lambda a: bool({"growth", "tech"} & set(a["tags"])))
        b.add("growth_low", "growth", "low", "add", "Little growth exposure for your objective",
              f"{ax['growth']['explanation']} The target for your profile is {ax['growth']['target']:.0f}%.",
              b.candidates("growth", pool, lambda r: "growth exposure that fits your profile"), ax["growth"]["evidence"], gap=g)

    g = gap("currency")
    if g is not None and abs(g) > 20:
        want = "CAD" if g > 0 else "USD"
        pool = b.eligible(lambda a, c=want: a["listing_currency"] == c)
        b.add("currency", "currency", "low", "add", f"Currency mix: consider more {want}-listed assets",
              f"{ax['currency']['explanation']} Target for your profile: {ax['currency']['target']:.0f}% USD.",
              b.candidates("currency", pool, lambda r: f"{want}-listed"), ax["currency"]["evidence"], gap=g)

    g = gap("geography")
    if g is not None and g < -15:
        shares = {e["claim"].split()[0]: e["value"] for e in ax["geography"]["evidence"]}
        region = min(shares, key=shares.get)
        pool = b.eligible(lambda a, r=region: a["region"] == r)
        b.add("geography", "geography", "low", "add", f"Little {region} exposure",
              ax["geography"]["explanation"],
              b.candidates("geography", pool, lambda r: f"{r['region']}-listed"), ax["geography"]["evidence"], gap=g)

    if ax["esg"]["target"] is not None:
        unscreened = sum(w for t, w in b.weights.items() if "unscreened_index" in assets[t]["esg"]["flags"])
        if unscreened > 0.2:
            pool = b.eligible(lambda a: "esg" in a["tags"])
            b.add("esg_funds", "esg", "low", "review", "Index funds are not ESG-screened",
                  f"{unscreened:.0%} of the portfolio is in funds that are not ESG-screened and may hold companies your screen excludes. "
                  f"Holdings look-through is not loaded yet, so the exact exposure is unknown.",
                  b.candidates("esg", pool, lambda r: "ESG-screened fund"), gap=unscreened * 100)

    g = gap("income")
    if g is not None and g < -15:
        pool = b.eligible(lambda a: "dividend" in a["tags"])
        b.add("income", "income", "low", "add", "Income is below the target for your profile",
              ax["income"]["explanation"], b.candidates("income", pool, lambda r: "regular dividend payer"),
              ax["income"]["evidence"], gap=g)

    g = gap("event_exposure")
    if g is not None and g > 0:
        b.add("events", "event_exposure", "medium", "review", "Recent high-severity events touch your holdings",
              ax["event_exposure"]["explanation"], evidence=ax["event_exposure"]["evidence"], gap=g)


def top_picks(scores: dict, n: int = 5, max_per_group: int = 2) -> list[dict]:
    """Highest fit scores with status OK and room under the cap, at most
    `max_per_group` per asset group so the list itself stays diversified."""
    picks, per_group = [], {}
    for r in scores["results"]:
        if r["status"] != "ok":
            continue
        if r["current_weight"] >= r["cap"]["value"] - 1e-9:
            continue
        role = "defensive" if r["asset_group"] in ("bond", "gold") else r["asset_group"]
        if per_group.get(role, 0) >= max_per_group:
            continue
        per_group[role] = per_group.get(role, 0) + 1
        comps = [c for c in r["components"] if c.get("applicable", True) and c["score"] is not None
                 and c["key"] not in ("news_events", "event_signal")]

        def is_strong(c):  # risk/loss only count as a reason when fully inside the tolerance
            return c["score"] >= 99.5 if c["key"] in ("risk_fit", "loss_fit") else c["score"] >= 70

        strong = sorted((c for c in comps if is_strong(c)), key=lambda c: -c["contribution"])
        drivers = strong[:2] or sorted(comps, key=lambda c: -c["score"])[:1]
        weak = sorted((c for c in comps if c["score"] < 60 or (c["key"] in ("risk_fit", "loss_fit") and c["score"] < 99.5)),
                      key=lambda c: c["score"])
        m = r["metrics"]
        risks = [f"Weaker point, {c['label'].lower()}: {c['explanation']}" for c in weak[:1]]
        if m.get("max_drawdown") is not None:
            risks.append(f"Worst peak-to-trough fall in {r['period']}: {abs(m['max_drawdown']):.0%}.")
        stress = (m.get("stress") or {}).get("2022_rates_shock")
        if stress is not None:
            risks.append(f"2022 (stocks and bonds fell together): {stress:+.0%}.")
        risks.append("Past behaviour does not predict future returns.")
        picks.append({
            "ticker": r["ticker"], "name": r["name"], "score": r["score"], "current_weight": r["current_weight"],
            "asset_group": r["asset_group"], "verdict": r.get("verdict"), "rank_stability": r.get("rank_stability"),
            "why": [f"{c['label']}: {c['explanation']}" for c in drivers],
            "what_could_go_wrong": risks,
            "confidence": r["confidence"],
        })
        if len(picks) == n:
            break
    return picks
