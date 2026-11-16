"""Profile-Fit Score (0-100): how well each asset matches one investor profile.

Two layers, both deterministic (the LLM never computes these numbers):
1. Asset features (asset_features.json): returns, volatility, drawdowns, risk-adjusted
   returns, technical indicators, fundamentals, news and filings, regimes.
2. Investor features (profile_features.py): tolerances, needs and component weights.

Each of nine components compares features with the investor's targets and returns a 0-100
score, a plain-language explanation and the evidence it used (value, source, period). The
total is the weighted sum, then two gates apply:
- an asset excluded by a stated preference is capped at 20 and marked "excluded";
- an asset far outside the risk or loss tolerance is capped at 45, so strengths elsewhere
  cannot hide a risk mismatch.
Components that do not apply (e.g. company fundamentals for a fund) or lack data are left
out and their weight is shared among the others; the result says so.

When asset_features.json is not loaded (placeholder fixture), components fall back to the
window metrics and confidence is lowered.
"""

from __future__ import annotations

import math
import os
import random
import statistics
from datetime import date, timedelta

from .data.provider import HEURISTIC_CORR_NOTE, DataProvider
from .portfolio import PortfolioStats, add_position, weights_from_holdings
from .profile_features import COMPONENTS, investor_features
from .profiles import RISK_LEVELS, applicable_cap, effective_caps, horizon_label, profile_assumptions

COMPONENT_LABELS = COMPONENTS
EXCLUDED_CAP = 20
RISK_GATE = 30         # risk or loss fit below this ...
RISK_GATE_CAP = 45     # ... caps the total here
SENSITIVITY_DRAWS = 300
SENSITIVITY_SPREAD = 0.25
WHAT_IF_WEIGHT = 0.05
NEWS_LOOKBACK_DAYS = 90
STALE_DAYS = 10
ESG_LABELS = {"fossil_fuels": "fossil fuels"}
VERDICTS = [(70, "strong_fit", "Strong fit"), (55, "good_fit", "Good fit"), (40, "partial_fit", "Partial fit"),
            (0, "poor_fit", "Poor fit")]
KEY_FEATURES = ("ret_3m", "ret_1y", "vol_1y", "mdd_3y", "mdd_5y", "sharpe_3y", "rsi_14", "px_vs_sma_200",
                "dividend_yield_ttm", "news_count_30d", "news_sentiment_30d", "pe", "market_cap", "garch_vol_1m",
                "p_turbulent", "esg_controversy_level")


def _soft(excess_ratio: float, softness: float = 0.5) -> float:
    """100 at zero excess, smoothly falling towards 0."""
    return 100.0 / (1.0 + (max(excess_ratio, 0.0) / softness) ** 2)


def _lin(x: float | None, lo: float, hi: float) -> float | None:
    """0 at lo, 100 at hi (works for hi < lo too), clipped."""
    if x is None:
        return None
    t = (x - lo) / (hi - lo)
    return 100.0 * max(0.0, min(1.0, t))


def _mean(parts: list[tuple[float | None, float]]) -> float | None:
    got = [(s, w) for s, w in parts if s is not None]
    if not got:
        return None
    return sum(s * w for s, w in got) / sum(w for _, w in got)


def _pct(x, digits=0):
    return "n/a" if x is None else f"{x:.{digits}%}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


class ScoringContext:
    """Everything shared across assets for one profile and window."""

    def __init__(self, profile: dict, provider: DataProvider, window: str | None):
        self.profile = profile
        self.provider = provider
        self.window, self.window_note = provider.resolve_window(window)
        self.period = provider.window_period(self.window)
        self.weights = weights_from_holdings(profile["holdings"])
        self.stats = PortfolioStats(self.weights, provider, self.window)
        self.base_vol = self.stats.volatility() if self.weights else None
        self.caps = effective_caps(profile)
        self.risk = RISK_LEVELS[profile["risk_level"]]
        self.inv = investor_features(profile, provider)
        self.tolerance = self.inv["drawdown_tolerance"]
        self.has_features = provider.has_features
        self.as_of = provider.features_as_of or date.today().isoformat()
        self.feature_period = self._feature_period()
        self.defensive_share = self.stats.share(lambda a: a["is_defensive"])
        self.region_share = {r: self.stats.share(lambda a, r=r: a["region"] == r) for r in ("CA", "US", "Global")}
        self.crypto_share = self.stats.share(lambda a: a["asset_group"] == "crypto")
        self.tech_share = self.stats.share(lambda a: bool({"tech", "growth"} & set(a["tags"])))
        self.sector_share: dict[str, float] = {}
        for t, w in self.weights.items():
            a = provider.assets[t]
            if a["asset_group"] == "equity":
                self.sector_share[a["sector"]] = self.sector_share.get(a["sector"], 0.0) + w

    def _feature_period(self) -> str:
        th = self.provider.data_through
        if not self.has_features:
            return self.period
        return f"to {self.as_of}" + (f" (live data through {th['live']})" if th.get("live") else "")

    # Feature with a fallback to the window metrics (placeholder fixture or missing feature).
    def f(self, t: str, key: str, fallback: str | None = None):
        v = self.provider.feature(t, key) if self.has_features else None
        if v is None and fallback:
            m = self.provider.metrics(t, self.window) or {}
            v = (m.get("stress") or {}).get("2022_rates_shock") if fallback == "stress" else m.get(fallback)
            if v is not None:
                return v, self.provider.evidence_ref(t, self.window, fallback if fallback != "stress" else "stress.2022_rates_shock"), self.period
        return v, self.provider.feature_ref(t, key), self.feature_period

    def ev(self, t: str, claim: str, key: str, unit: str, fallback: str | None = None) -> tuple:
        v, src, period = self.f(t, key, fallback)
        return v, {"claim": claim, "value": v, "source_id": src, "period": period, "unit": unit}


def _ev(claim: str, value, source_id: str, period: str | None, unit: str | None = None) -> dict:
    return {"claim": claim, "value": value, "source_id": source_id, "period": period, "unit": unit}


# ----- components ----------------------------------------------------------------
def _risk_fit(t: str, ctx: ScoringContext) -> dict:
    lo, hi = ctx.risk["vol_band"]
    v1, e1 = ctx.ev(t, "Volatility, 1 year", "vol_1y", "fraction")
    vl, el = None, None
    m = ctx.provider.metric(t, ctx.window, "ann_vol")
    if m is not None:
        vl, el = m, _ev(f"Volatility, {ctx.window} window", m, ctx.provider.evidence_ref(t, ctx.window, "ann_vol"), ctx.period, "fraction")
    vg, eg = ctx.ev(t, "GARCH(1,1) volatility forecast, next month", "garch_vol_1m", "fraction")
    mix = {"short": (0.45, 0.35, 0.20), "medium": (0.35, 0.15, 0.50), "long": (0.20, 0.10, 0.70)}[ctx.inv["horizon_class"]]
    parts = [(v1, mix[0]), (vg, mix[1]), (vl, mix[2])]
    vol = _mean(parts)
    if vol is None:
        return {"score": 50.0, "explanation": "No volatility data; neutral score.", "evidence": [], "missing": True}
    evidence = [e for e in (e1, eg, el) if e and e["value"] is not None]
    label = ctx.risk["label"].lower()
    used = ", ".join(f"{n} {_pct(v)}" for n, v in (("1-year", v1), ("next-month forecast", vg), (f"{ctx.window}", vl)) if v is not None)
    if vol > hi:
        score = _soft((vol - hi) / hi)
        how = "slightly above" if score >= 80 else "above" if score >= 40 else "far above"
        text = f"Blended volatility of {vol:.0%} a year ({used}) is {how} the {lo:.0%} to {hi:.0%} band for a {label} profile."
    elif vol < lo:
        score = _soft((lo - vol) / lo, ctx.risk["below_softness"])
        text = f"Blended volatility of {vol:.0%} ({used}) is below the {lo:.0%} to {hi:.0%} band: steadier than this profile targets, which may also limit growth."
    else:
        score = 100.0
        text = f"Blended volatility of {vol:.0%} a year ({used}) is inside the {lo:.0%} to {hi:.0%} band for a {label} profile."
    pt, ept = ctx.ev(t, "Probability of the turbulent volatility regime", "p_turbulent", "fraction")
    if pt is not None and pt > 0.6:
        evidence.append(ept)
        text += f" The regime model puts a {pt:.0%} probability on a turbulent phase right now."
        if ctx.inv["objective"] == "preservation":
            score = max(0.0, score - 10)
            text += " For a capital-preservation profile this lowers the score by 10 points."
    text += f" Weights of the blend follow your {ctx.inv['horizon_class']} horizon."
    return {"score": score, "explanation": text, "evidence": evidence, "facts": {"blended_vol": round(vol, 4)}}


def _loss_fit(t: str, ctx: ScoringContext) -> dict:
    inv = ctx.inv
    # The longest window is used for every horizon: the horizon sets the tolerance, not how much
    # history is looked at, and the 5-year window is the only one that contains the 2022 shock.
    dd, edd = ctx.ev(t, "Maximum drawdown, 5 years", "mdd_5y", "fraction", "max_drawdown")
    st, est = ctx.ev(t, "Calendar-2022 return", "stress_2022", "fraction", "stress")
    rec, erec = ctx.ev(t, "Recovery time after the worst fall (trading days)", "recovery_days_5y", "days", "recovery_days")
    es, ees = ctx.ev(t, "Expected shortfall (CVaR), 95%, one day", "es_95_1d", "fraction")
    if dd is None:
        return {"score": 50.0, "explanation": "No drawdown data; neutral score.", "evidence": [], "missing": True}
    tol, st_tol, budget = inv["drawdown_tolerance"], inv["stress_tolerance"], inv["recovery_budget_days"]
    fall = abs(dd)
    s_dd = 100.0 if fall <= tol else _soft((fall - tol) / tol)
    s_st = None if st is None else (100.0 if st >= -st_tol else _soft((-st - st_tol) / st_tol))
    if rec is not None:
        s_rec = 100.0 if rec <= budget else _soft((rec - budget) / budget)
    elif fall > 0.05 and ctx.has_features:
        s_rec = 25.0
    else:
        s_rec = None
    score = _mean([(s_dd, 0.5), (s_st, 0.3), (s_rec, 0.2)])
    hz = horizon_label(inv["horizon_years"])
    bits = [f"Worst fall {fall:.0%} vs your {tol:.0%} tolerance ({'within' if fall <= tol else 'beyond'})"]
    if st is not None:
        bits.append(f"2022 return {st:+.0%} vs a {st_tol:.0%} stress tolerance")
    if rec is not None:
        bits.append(f"recovered in {rec} trading days vs a budget of {budget} for a {hz} horizon")
    elif s_rec is not None:
        bits.append("had not recovered its prior peak by the end of the data")
    text = "; ".join(bits) + "."
    if es is not None:
        text += f" On the worst 5% of days it lost {es:.1%} on average."
    ev = [e for e in (edd, est, erec, ees) if e["value"] is not None]
    return {"score": score, "explanation": text, "evidence": ev}


def _return_quality(t: str, ctx: ScoringContext) -> dict:
    sh, esh = ctx.ev(t, "Sharpe ratio, 3 years", "sharpe_3y", "ratio")
    so, eso = ctx.ev(t, "Sortino ratio, 3 years", "sortino_3y", "ratio")
    if sh is None:
        sh, esh = ctx.ev(t, "Sharpe ratio, 1 year", "sharpe_1y", "ratio")
    cg, ecg = ctx.ev(t, "Compound annual growth, 5 years", "cagr_5y", "fraction", "ann_return")
    if sh is None and so is None:
        if cg is None:
            return {"score": 50.0, "explanation": "No return history; neutral score.", "evidence": [], "missing": True}
        vol = ctx.provider.metric(t, ctx.window, "ann_vol") or 0.2
        sh = cg / vol if vol else 0
    raw = _mean([(_lin(sh, -0.5, 1.5), 0.5), (_lin(so, -0.5, 2.2), 0.5)])
    score = 50 + (raw - 50) * 0.6
    se = math.sqrt((1 + 0.5 * sh * sh) / 3)
    text = (f"Sharpe ratio {sh:.2f}" + (f", Sortino {so:.2f}" if so is not None else "") +
            (f", 5-year growth {cg:.1%} a year" if cg is not None else "") +
            f". Past risk-adjusted returns are a noisy guide (a 3-year Sharpe ratio has a standard error of about "
            f"±{se:.1f}), so this score is pulled 40% toward neutral.")
    return {"score": score, "explanation": text, "evidence": [e for e in (esh, eso, ecg) if e["value"] is not None]}


def _fundamentals(t: str, ctx: ScoringContext, asset: dict) -> dict:
    if asset["asset_group"] != "equity":
        return {"applicable": False, "score": None, "evidence": [],
                "explanation": "Not applicable: company fundamentals do not exist for a fund or a crypto asset."}
    if not ctx.has_features:
        return {"applicable": False, "score": None, "evidence": [], "explanation": "Fundamentals are not loaded."}
    fin = asset["sector"] == "Financials"
    g = {k: ctx.ev(t, lbl, k, unit) for k, lbl, unit in (
        ("roe", "Return on equity", "fraction"), ("roic", "Return on invested capital", "fraction"),
        ("earnings_growth", "Earnings growth", "fraction"), ("revenue_growth", "Revenue growth", "fraction"),
        ("debt_to_equity", "Debt / equity", "ratio"), ("net_debt_ebitda", "Net debt / EBITDA", "ratio"),
        ("fcf_yield", "Free cash flow yield", "fraction"), ("pe", "Price / earnings", "ratio"), ("pb", "Price / book", "ratio"))}
    v = {k: x[0] for k, x in g.items()}
    available = [k for k, x in v.items() if x is not None]
    if len(available) < 3:
        reason = next(iter(ctx.provider.feature_missing(t).get(k) for k in ("roe", "pe") if ctx.provider.feature_missing(t).get(k)), None)
        return {"applicable": False, "score": None, "evidence": [],
                "explanation": "Not enough fundamentals to score" + (f": {reason}" if reason else ".")}
    roe = None if v["roe"] is None else min(v["roe"], 0.40)
    prof = _mean([(_lin(roe, 0.0, 0.20), 1.0), (None if fin else _lin(v["roic"], 0.0, 0.15), 1.0)])
    growth = _mean([(_lin(v["earnings_growth"], -0.20, 0.15), 0.6), (_lin(v["revenue_growth"], -0.05, 0.12), 0.4)])
    lev = None if fin else _mean([(_lin(v["debt_to_equity"], 2.5, 0.3), 0.5), (_lin(v["net_debt_ebitda"], 4.0, 0.5), 0.5)])
    cash = None if fin else _lin(v["fcf_yield"], 0.0, 0.05)
    pe = v["pe"]
    if pe is None:
        val = None
    elif pe < 8:
        val = 70.0
    elif pe <= 25:
        val = 100.0
    else:
        val = _lin(pe, 80, 25)
    if fin and v["pb"] is not None:
        val = _mean([(val, 0.5), (_lin(v["pb"], 4.0, 1.0), 0.5)])
    val_w = 0.25 if ctx.inv["needs"]["quality"] or ctx.inv["objective"] == "preservation" else 0.15
    score = _mean([(prof, 0.30), (growth, 0.20), (lev, 0.20), (cash, 0.15), (val, val_w)])
    notes = []
    if v["roe"] is not None:
        notes.append(f"ROE {v['roe']:.0%}" + (" (capped at 40% for scoring; buybacks can inflate it)" if v["roe"] > 0.40 else ""))
    if v["earnings_growth"] is not None:
        notes.append(f"earnings growth {v['earnings_growth']:+.0%}")
    if not fin and v["debt_to_equity"] is not None:
        notes.append(f"debt/equity {v['debt_to_equity']:.2f}")
    if not fin and v["fcf_yield"] is not None:
        notes.append(f"FCF yield {v['fcf_yield']:.1%}")
    if pe is not None:
        notes.append(f"P/E {pe:.0f}" + (" (stretched)" if pe > 40 else ""))
    if fin:
        notes.append("leverage and cash-flow ratios are left out for a bank or insurer")
    det = ctx.provider.feature_details(t).get("fundamentals") or {}
    period = f"{det.get('fiscal_year')} ({det.get('form')} filed {det.get('filed')})" if det else ctx.feature_period
    ev = []
    for k in available:
        e = dict(g[k][1])
        e["period"] = period
        ev.append(e)
    return {"score": score, "explanation": "Latest fiscal year from SEC filings: " + ", ".join(notes) + ".", "evidence": ev}


def _income(t: str, ctx: ScoringContext, asset: dict) -> dict:
    y, ey = ctx.ev(t, "Dividend yield, trailing 12 months", "dividend_yield_ttm", "fraction", "dividend_yield_ttm")
    dg, edg = ctx.ev(t, "Dividend growth, 3 years (annual rate)", "dividend_growth_3y", "fraction")
    po, epo = ctx.ev(t, "Payout ratio", "payout_ratio", "fraction")
    if y is None:
        return {"applicable": False, "score": None, "evidence": [], "explanation": "No dividend data."}
    s_y = _lin(y, 0.0, 0.04)
    s_g = _lin(dg, -0.05, 0.08) if y > 0 else None
    s_p = None if po is None else (100.0 if po <= 0.7 else _lin(po, 1.2, 0.7))
    score = _mean([(s_y, 0.6), (s_g, 0.25), (s_p, 0.15)])
    text = f"Trailing dividend yield {y:.1%}"
    if dg is not None:
        text += f", dividends grew {dg:+.1%} a year over 3 years"
    if po is not None:
        text += f", payout ratio {po:.0%}" + (" (above 100% of earnings: harder to sustain)" if po > 1 else "")
    text += "." + ("" if ctx.inv["needs"]["income"] else " Income carries little weight for your profile.")
    return {"score": score, "explanation": text, "evidence": [e for e in (ey, edg, epo) if e["value"] is not None]}


def _momentum(t: str, ctx: ScoringContext) -> dict:
    if not ctx.has_features:
        return {"applicable": False, "score": None, "evidence": [], "explanation": "Technical indicators are not loaded."}
    p200, e200 = ctx.ev(t, "Price vs 200-day average", "px_vs_sma_200", "fraction")
    s50, _ = ctx.ev(t, "SMA 50", "sma_50", "price")
    s200, _ = ctx.ev(t, "SMA 200", "sma_200", "price")
    rsi, ersi = ctx.ev(t, "RSI, 14 days", "rsi_14", "index")
    mh, emh = ctx.ev(t, "MACD histogram / price", "macd_hist_pct", "fraction")
    rel, erel = ctx.ev(t, "1-year return vs sector peers", "rel_sector_1y", "fraction")
    if rel is None:
        rel, erel = ctx.ev(t, "1-year return vs home market", "rel_market_1y", "fraction")
    if p200 is None and rsi is None:
        return {"applicable": False, "score": None, "evidence": [], "explanation": "Not enough history for trend indicators."}
    score, bits = 50.0, []
    if p200 is not None:
        score += 15 if p200 > 0 else -15
        bits.append(f"price {abs(p200):.0%} {'above' if p200 > 0 else 'below'} its 200-day average")
    if s50 and s200:
        score += 10 if s50 > s200 else -10
        bits.append("50-day average above the 200-day (uptrend)" if s50 > s200 else "50-day average below the 200-day (downtrend)")
    if mh is not None:
        score += 5 if mh > 0 else -5
        bits.append(f"MACD histogram {'positive' if mh > 0 else 'negative'}")
    if rsi is not None:
        if rsi > 75:
            score -= 10 if ctx.inv["objective"] == "preservation" else 5
            bits.append(f"RSI {rsi:.0f}: overbought after a sharp rise")
        elif rsi < 25:
            score -= 5
            bits.append(f"RSI {rsi:.0f}: oversold after a sharp fall")
        else:
            bits.append(f"RSI {rsi:.0f} (neutral range)")
    if rel is not None:
        score += max(-10.0, min(10.0, rel / 0.3 * 10))
        bits.append(f"1-year return {rel * 100:+.0f} points vs {'sector peers' if 'sector' in erel['claim'] else 'its home market'}")
    text = _cap("; ".join(bits)) + ". Trend indicators describe the recent past; they are not a forecast."
    ev = [e for e in (e200, ersi, emh, erel) if e["value"] is not None]
    return {"score": max(0.0, min(100.0, score)), "explanation": text, "evidence": ev}


def _preference_fit(asset: dict, ctx: ScoringContext) -> tuple[dict, list[str]]:
    p, inv = ctx.profile, ctx.inv
    t = asset["ticker"]
    score, notes, exclusions = 50.0, [], []
    tags = set(asset["tags"]) | ({"defensive"} if asset["is_defensive"] else set())
    matches = sorted(set(p["interests"]) & tags)
    if matches:
        score += min(40, 15 * len(matches))
        notes.append(f"matches your interests: {', '.join(matches)}")
    group = asset["asset_group"]
    if group == "crypto":
        if p["crypto"] == "avoid" or ctx.caps["max_crypto"] == 0:
            exclusions.append("crypto is outside your stated preferences (crypto cap 0%)")
        elif p["crypto"] == "limited":
            score -= 25
            notes.append("crypto: you asked to keep it limited")
    objective = inv["objective"]
    if objective == "growth" and group == "bond":
        score -= 15
        notes.append("bonds mainly stabilise; they play a small role in a long-term growth objective")
    elif objective == "growth" and group == "equity":
        if {"growth", "emerging_tech"} & set(asset["tags"]):
            score += 10
            notes.append("growth-oriented, in line with a long-term growth objective")
        elif asset["is_defensive"]:
            score -= 10
            notes.append("defensive company: steadier, with less growth potential than your objective targets")
    elif objective == "preservation":
        if group == "crypto":
            score -= 20
            notes.append("crypto is speculative relative to a capital-preservation objective")
        elif "growth" in asset["tags"] and "established" not in asset["tags"]:
            score -= 10
            notes.append("growth-oriented company, more volatile than a capital-preservation objective implies")
        elif asset["is_defensive"] and "defensive" not in matches:
            score += 10
            notes.append("defensive asset supports capital preservation")
    if inv["needs"]["quality"] and group == "equity":
        if "established" in asset["tags"]:
            score += 5
            notes.append("established company, as you prefer")
        else:
            score -= 5
            notes.append("not an established, long-listed company")
    vol1 = ctx.provider.feature(t, "vol_1y") if ctx.has_features else ctx.provider.metric(t, ctx.window, "ann_vol")
    if inv["speculation_aversion"] == "high" and "high_profile" in asset["tags"] and (vol1 or 0) > 0.35:
        score -= 15
        notes.append("highly publicised and volatile: speculative relative to your stated preferences")
    if asset["sector"] in p["avoid_sectors"]:
        exclusions.append(f"sector {asset['sector']} is on your avoid list")
    for flag in sorted(set(p["esg_exclusions"]) & set(asset["esg"]["flags"])):
        exclusions.append(f"ESG screen: {ESG_LABELS.get(flag, flag)}")
    if p["esg_exclusions"] and "unscreened_index" in asset["esg"]["flags"]:
        score -= 10
        notes.append("fund is not ESG-screened and may hold companies your screen excludes")
    ev = [_ev("Asset tags", asset["tags"], f"assets.json#{t}.tags", None, "label")]
    if asset["esg"]["flags"] or asset["esg"].get("notes"):
        ev.append(_ev("ESG flags", asset["esg"]["flags"], f"assets.json#{t}.esg", asset["esg"].get("notes"), "label"))
    if inv["needs"]["esg"] != "none" and ctx.has_features:
        level = ctx.provider.feature(t, "esg_controversy_level")
        n = ctx.provider.feature(t, "esg_controversies_1y") or 0
        if level == "significant":
            score -= 30
            notes.append(f"significant ESG controversy evidence in the last year ({n} item(s) in filings and allow-listed news)")
        elif level == "minor":
            score -= 10
            notes.append(f"minor ESG controversy evidence in the last year ({n} item(s))")
        for c in (ctx.provider.feature_details(t).get("news") or {}).get("controversies", [])[:3]:
            ev.append(_ev(f"Controversy: {c['title']}", c.get("event_type"), c.get("source_url") or "signals.json",
                          (c.get("published_at") or "")[:10], "label"))
    if exclusions:
        score = 0.0
    score = max(0.0, min(100.0, score))
    text = "; ".join(exclusions + notes)
    text = (text[:1].upper() + text[1:]) if text else "No specific match with your stated preferences (neutral)"
    return {"score": score, "explanation": text + ".", "evidence": ev}, exclusions


def _diversification(asset: dict, ctx: ScoringContext) -> dict:
    t = asset["ticker"]
    heuristic, used_1y = False, False

    def corr(a, b):
        nonlocal heuristic, used_1y
        c = ctx.provider.correlation_1y(a, b) if ctx.has_features else None
        if c is not None:
            used_1y = True
            return c
        c, h = ctx.provider.correlation(a, b, ctx.window)
        heuristic |= h
        return c

    if ctx.weights:
        total = sum(ctx.weights.values())
        avg = sum(w * corr(t, h) for h, w in ctx.weights.items()) / total
        ref = "your current holdings"
    else:
        avg = corr(t, "SPY")
        ref = "SPY (market proxy; no holdings entered)"
    corr_score = 100.0 * (1.0 - max(avg, 0.0))
    dvol = None
    if ctx.weights and ctx.base_vol:
        after = PortfolioStats(add_position(ctx.weights, t, WHAT_IF_WEIGHT), ctx.provider, ctx.window).volatility()
        if after is not None:
            dvol = after - ctx.base_vol
    mr_score = None if dvol is None else (100.0 if dvol <= 0 else _soft(dvol / 0.005))
    score = _mean([(corr_score, 0.6), (mr_score, 0.4)])
    extras = []
    if ctx.weights and asset["is_defensive"] and ctx.defensive_share < ctx.caps["min_defensive"]:
        score += 10
        extras.append(f"adds defensive exposure (you hold {ctx.defensive_share:.0%}, minimum {ctx.caps['min_defensive']:.0%})")
    if ctx.weights and ctx.region_share.get(asset["region"], 0) < 0.15:
        score += 5
        extras.append(f"adds {asset['region']} exposure (currently {ctx.region_share.get(asset['region'], 0):.0%})")
    if ctx.weights and ctx.tech_share >= 0.5 and {"tech", "growth"} & set(asset["tags"]):
        score -= 15
        extras.append(f"adds to an existing concentration: {ctx.tech_share:.0%} of the portfolio is already in tech or growth assets")
    score = max(0.0, min(100.0, score))
    text = f"Weighted average correlation with {ref} is {avg:.2f}" + (" (weekly returns, last 12 months)." if used_1y else ".")
    if dvol is not None:
        text += f" Adding {WHAT_IF_WEIGHT:.0%} would move estimated portfolio volatility by {dvol * 100:+.2f} points."
    if extras:
        text += " " + "; ".join(e[:1].upper() + e[1:] for e in extras) + "."
    src = "asset_features.json#correlation_1y" if used_1y else ("heuristic:asset_group_priors" if heuristic else "correlations.json")
    ev = [_ev("Average correlation with reference", round(avg, 3), src, ctx.feature_period if used_1y else ctx.period, "ratio")]
    if dvol is not None:
        ev.append(_ev(f"Change in portfolio volatility if {WHAT_IF_WEIGHT:.0%} is added", round(dvol, 4),
                      "computed:asset_metrics.ann_vol+correlations", ctx.period, "fraction"))
    return {"score": score, "explanation": text, "evidence": ev, "heuristic": heuristic and not used_1y}


def _news_events(t: str, ctx: ScoringContext) -> dict:
    end = ctx.as_of
    start = (date.fromisoformat(end) - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
    items = ctx.provider.signals_between([t], start, end)
    news = [s for s in items if s.get("kind") == "news"]
    filings = [s for s in items if s.get("kind") == "filing"]
    if not items:
        return {"score": 50.0, "evidence": [], "no_data": True,
                "explanation": f"No filings or allow-listed news in the {NEWS_LOOKBACK_DAYS} days to {end} (neutral). "
                               "No coverage is not evidence of no risk."}
    sent = ctx.provider.feature(t, "news_sentiment_30d") if ctx.has_features else None
    if sent is None and news:
        sent = statistics.fmean(float(s.get("sentiment") or 0) for s in news[:10])
    mom = ctx.provider.feature(t, "news_sentiment_momentum") if ctx.has_features else None
    material = [s for s in items if s.get("materiality") == "high"]
    neg = [s for s in material if float(s.get("sentiment") or 0) < -0.15]
    buys = ctx.provider.feature(t, "insider_buys_90d") if ctx.has_features else None
    score = 50 + 35 * (sent or 0) + 10 * max(-1.0, min(1.0, (mom or 0) / 0.5)) - min(24, 8 * len(neg))
    if buys:
        score += 5
    score = max(0.0, min(100.0, score))
    bits = [f"{len(news)} article(s) and {len(filings)} filing(s) in {NEWS_LOOKBACK_DAYS} days"]
    if sent is not None:
        bits.append(f"recency-weighted headline sentiment {sent:+.2f}")
    if mom is not None:
        bits.append(f"sentiment {'improving' if mom > 0 else 'worsening'} vs the prior 60 days ({mom:+.2f})")
    if neg:
        bits.append(f"{len(neg)} negative high-materiality item(s), latest: \"{neg[0]['title']}\" ({neg[0].get('publisher')}, {neg[0]['published_at'][:10]})")
    elif material:
        bits.append(f"{len(material)} high-materiality item(s), latest: \"{material[0]['title']}\" ({material[0].get('publisher')}, {material[0]['published_at'][:10]})")
    if buys:
        bits.append(f"{buys} insider open-market purchase(s) on Form 4")
    text = _cap("; ".join(bits)) + ". Headlines inform the explanation; they do not predict returns."
    ev = [_ev(s["title"], s.get("sentiment"), s.get("source_url") or f"signals.json#{s['signal_id']}",
              f"{s.get('publisher')} · {s['published_at'][:10]}", "sentiment") for s in (neg + [x for x in items if x not in neg])[:5]]
    return {"score": score, "explanation": text, "evidence": ev}


# ----- assembly -------------------------------------------------------------------
def _verdict(score: int, status: str, gated: bool) -> dict:
    if status == "excluded":
        return {"key": "excluded", "label": "Excluded by your preferences"}
    if gated:
        return {"key": "outside_profile", "label": "Outside your risk profile"}
    for floor, key, label in VERDICTS:
        if score >= floor:
            return {"key": key, "label": label}
    return {"key": "poor_fit", "label": "Poor fit"}


def _combine(parts: dict, weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    use = {k: weights[k] for k, p in parts.items() if p.get("applicable", True) and p.get("score") is not None}
    tot = sum(use.values()) or 1.0
    eff = {k: w / tot for k, w in use.items()}
    return sum(eff[k] * parts[k]["score"] for k in eff), eff


def _gates(total: float, parts: dict, excluded: bool) -> tuple[float, list[str], bool]:
    notes, gated = [], False
    if parts["risk_fit"]["score"] < RISK_GATE or parts["loss_fit"]["score"] < RISK_GATE:
        if total > RISK_GATE_CAP:
            notes.append(f"Score capped at {RISK_GATE_CAP} because volatility or loss fit is below {RISK_GATE}: "
                         "strengths elsewhere do not offset a risk mismatch.")
        total, gated = min(total, RISK_GATE_CAP), True
    if excluded:
        if total > EXCLUDED_CAP:
            notes.append(f"Score capped at {EXCLUDED_CAP} because the asset is excluded by your preferences.")
        total = min(total, EXCLUDED_CAP)
    return total, notes, gated


def score_asset(ticker: str, ctx: ScoringContext) -> dict:
    asset = ctx.provider.assets[ticker]
    pref, exclusions = _preference_fit(asset, ctx)
    parts = {
        "risk_fit": _risk_fit(ticker, ctx),
        "loss_fit": _loss_fit(ticker, ctx),
        "return_quality": _return_quality(ticker, ctx),
        "fundamentals": _fundamentals(ticker, ctx, asset),
        "income": _income(ticker, ctx, asset),
        "momentum": _momentum(ticker, ctx),
        "preference_fit": pref,
        "diversification": _diversification(asset, ctx),
        "news_events": _news_events(ticker, ctx),
    }
    raw, eff = _combine(parts, ctx.inv["weights"])
    total, gate_notes, gated = _gates(raw, parts, bool(exclusions))
    components = []
    for key, part in parts.items():
        applicable = key in eff
        w = eff.get(key, 0.0)
        components.append({
            "key": key, "label": COMPONENT_LABELS[key], "weight": round(w, 4),
            "profile_weight": ctx.inv["weights"][key], "applicable": applicable,
            "score": None if part.get("score") is None else round(part["score"], 1),
            "contribution": round(w * part["score"], 1) if applicable else 0.0,
            "explanation": part["explanation"], "evidence": part["evidence"],
        })

    weight = ctx.weights.get(ticker, 0.0)
    cap_name, cap = applicable_cap(asset, ctx.caps)
    badges, status = [], "ok"
    if gated:
        status = "caution"
        badges.append("Outside risk profile")
    held = ctx.crypto_share if asset["asset_group"] == "crypto" else weight
    what = "Crypto" if asset["asset_group"] == "crypto" else "Position"
    if held > 0 and held >= cap - 1e-9 and cap > 0:
        status = "over_cap" if held > cap + 0.005 else "cap_reached"
        badges.append(f"{what} {'over' if status == 'over_cap' else 'at'} cap: {held:.0%} held vs {cap:.0%} limit")
    elif asset["asset_group"] == "equity":
        sector_share = ctx.sector_share.get(asset["sector"], 0.0)
        if sector_share >= ctx.caps["max_sector"] - 1e-9:
            status = "cap_reached"
            badges.append(f"Sector at cap: {asset['sector']} stocks are {sector_share:.0%} vs {ctx.caps['max_sector']:.0%} limit")
    neg_news = ctx.provider.feature(ticker, "negative_material_90d") if ctx.has_features else None
    if neg_news:
        badges.append(f"{neg_news} negative material event(s) in 90 days")
    if exclusions:
        status = "excluded"
        badges = [f"Excluded: {e}" for e in exclusions] + badges

    score = int(round(total))
    ranked = sorted((c for c in components if c["applicable"]), key=lambda c: (c["score"] - 50) * c["weight"])
    drivers = {
        "up": [{"key": c["key"], "label": c["label"], "explanation": c["explanation"]}
               for c in reversed(ranked) if c["score"] >= 60][:2],
        "down": [{"key": c["key"], "label": c["label"], "explanation": c["explanation"]}
                 for c in ranked if c["score"] < 45][:2],
    }
    m = ctx.provider.metrics(ticker, ctx.window) or {}
    feats = ctx.provider.features(ticker) if ctx.has_features else {}
    news = (ctx.provider.feature_details(ticker).get("news") or {}).get("latest", [])[:5] if ctx.has_features else []
    return {
        "ticker": ticker, "name": asset["name"], "asset_class": asset["asset_class"], "asset_group": asset["asset_group"],
        "region": asset["region"], "sector": asset["sector"], "listing_currency": asset["listing_currency"], "tags": asset["tags"],
        "score": score, "raw_score": round(raw, 1), "verdict": _verdict(score, status, gated),
        "status": status, "badges": badges, "gates": gate_notes,
        "current_weight": round(weight, 4), "cap": {"name": cap_name, "value": cap},
        "metrics": {
            "ann_return": m.get("ann_return"), "ann_vol": m.get("ann_vol"), "max_drawdown": m.get("max_drawdown"),
            "dividend_yield_ttm": m.get("dividend_yield_ttm"), "stress": m.get("stress") or {},
        },
        "key_features": {k: feats.get(k) for k in KEY_FEATURES},
        "components": components, "drivers": drivers, "news": news,
        "confidence": _confidence(ticker, ctx, parts),
        "window": ctx.window, "period": ctx.period, "features_as_of": ctx.as_of if ctx.has_features else None,
    }


def _confidence(ticker: str, ctx: ScoringContext, parts: dict) -> dict:
    level, reasons = 3, []
    if ctx.provider.is_placeholder:
        level = 1
        reasons.append("Metrics are placeholder values, not pipeline output.")
    if not ctx.has_features:
        level = min(level, 2)
        reasons.append("Feature engine output (asset_features.json) is not loaded; only window metrics are used.")
    if ctx.window_note:
        level -= 1
        reasons.append(ctx.window_note)
    if parts["diversification"].get("heuristic"):
        level -= 1
        reasons.append(HEURISTIC_CORR_NOTE)
    history = ctx.provider.metric(ticker, ctx.window, "history_days")
    if history is not None and history < 750:
        level -= 1
        reasons.append(f"Short history ({history} trading days).")
    if ctx.inv["horizon_beyond_data"]:
        level -= 1
        reasons.append(f"Your horizon is {ctx.inv['horizon_years']} years; risk measures describe at most 5 years of data "
                       "(2021-2026), including one rate shock.")
    if ctx.has_features:
        age = (date.today() - date.fromisoformat(ctx.as_of)).days
        if age > int(os.environ.get("ALPHAFORGE_STALE_DAYS") or STALE_DAYS):
            level -= 1
            reasons.append(f"Market data ends on {ctx.as_of} ({age} days ago); run the market_data ingest job to refresh.")
    if parts["news_events"].get("no_data"):
        reasons.append("No recent news or filings found in the sources searched; the news component is neutral.")
    if parts["risk_fit"].get("missing") or parts["loss_fit"].get("missing"):
        level = 1
        reasons.append("Volatility or drawdown is missing for this asset.")
    level = max(level, 1)
    return {"level": {3: "high", 2: "medium", 1: "low"}[level], "reasons": reasons}


def _sensitivity(results: list[dict], weights: dict[str, float], top_n: int = 5) -> None:
    """Re-rank under perturbed weights; attach how stable each asset's rank is."""
    rng = random.Random(42)
    comps = {r["ticker"]: {c["key"]: (c["score"], c["applicable"]) for c in r["components"]} for r in results}
    gates = {r["ticker"]: (r["status"] == "excluded",
                           any(c["key"] in ("risk_fit", "loss_fit") and c["score"] is not None and c["score"] < RISK_GATE
                               for c in r["components"])) for r in results}
    ranks: dict[str, list[int]] = {t: [] for t in comps}
    for _ in range(SENSITIVITY_DRAWS):
        w = {k: v * rng.uniform(1 - SENSITIVITY_SPREAD, 1 + SENSITIVITY_SPREAD) for k, v in weights.items()}
        totals = {}
        for t, cs in comps.items():
            use = {k: w[k] for k, (s, ok) in cs.items() if ok and s is not None}
            tot = sum(use.values()) or 1
            val = sum(use[k] / tot * cs[k][0] for k in use)
            excluded, gated = gates[t]
            if gated:
                val = min(val, RISK_GATE_CAP)
            if excluded:
                val = min(val, EXCLUDED_CAP)
            totals[t] = (excluded, -val)
        order = sorted(totals, key=lambda t: (totals[t], t))
        for i, t in enumerate(order):
            ranks[t].append(i + 1)
    for r in results:
        rk = sorted(ranks[r["ticker"]])
        n = len(rk)
        r["rank_stability"] = {
            "median_rank": rk[n // 2], "rank_p10": rk[int(n * 0.1)], "rank_p90": rk[int(n * 0.9) - 1],
            "p_top5": round(sum(1 for x in rk if x <= top_n) / n, 3),
            "p_bottom5": round(sum(1 for x in rk if x > len(results) - top_n) / n, 3),
        }


def score_universe(profile: dict, provider: DataProvider, window: str | None = None) -> dict:
    ctx = ScoringContext(profile, provider, window)
    results = [score_asset(t, ctx) for t in provider.assets]
    results.sort(key=lambda r: (r["status"] == "excluded", -r["score"], r["ticker"]))
    for i, r in enumerate(results):
        r["rank"] = i + 1
    _sensitivity(results, ctx.inv["weights"])
    w = ctx.inv["weights"]
    formula = " + ".join(f"{w[k]:.0%} {COMPONENT_LABELS[k].lower()}" for k in sorted(w, key=lambda k: -w[k]))
    return {
        "window": ctx.window,
        "period": ctx.period,
        "window_note": ctx.window_note,
        "features_as_of": ctx.as_of if ctx.has_features else None,
        "data_through": provider.data_through,
        "component_weights": w,
        "component_labels": COMPONENT_LABELS,
        "investor": ctx.inv,
        "market_regime": provider.market_regime(),
        "results": results,
        "sensitivity": {"draws": SENSITIVITY_DRAWS, "spread": SENSITIVITY_SPREAD,
                        "method": f"Each weight is multiplied by a random factor between {1 - SENSITIVITY_SPREAD:.2f} and "
                                  f"{1 + SENSITIVITY_SPREAD:.2f} and the ranking is recomputed {SENSITIVITY_DRAWS} times "
                                  "(fixed seed). p_top5 is the share of draws in which the asset ranks in the top five."},
        "assumptions": profile_assumptions(profile) + [
            f"Score = {formula} (weights from your profile; components that do not apply are left out and their weight shared).",
            f"Gates: excluded assets are capped at {EXCLUDED_CAP}; volatility or loss fit below {RISK_GATE} caps the score at {RISK_GATE_CAP}.",
            "Past volatility, drawdowns and returns describe the data window; they do not predict future returns.",
        ],
    }
