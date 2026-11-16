"""How the app reads an investor profile: classifications, tolerances and factor weights.

Every value here is a documented team assumption for a simulation, not a suitability rule.
The result is returned with each ranking, so a user can see which reading of their profile
drove the scores and change it (risk level, horizon, interests, exclusions).

Weights: profiles differ mainly through their tolerances (volatility band, loss tolerance,
caps). The weights add a second, smaller difference: what matters more for this kind of
investor. They start from a base set per objective, are adjusted by horizon and stated needs,
and are normalised to 100%. The ranking's sensitivity test perturbs them to show how much
the order depends on these choices.
"""

from __future__ import annotations

from .portfolio import weights_from_holdings
from .profiles import RISK_LEVELS, effective_caps, profile_drawdown_tolerance

COMPONENTS = {
    "risk_fit": "Volatility fit",
    "loss_fit": "Loss and recovery fit",
    "return_quality": "Risk-adjusted return",
    "fundamentals": "Business quality",
    "income": "Income",
    "momentum": "Trend and timing",
    "preference_fit": "Preference fit",
    "diversification": "Diversification contribution",
    "news_events": "News and events",
}

OBJECTIVE_BY_RISK = {"high": "growth", "moderate": "balanced", "low_moderate": "preservation", "low": "preservation"}
BASE_WEIGHTS = {
    "preservation": {"risk_fit": 0.24, "loss_fit": 0.20, "return_quality": 0.06, "fundamentals": 0.10, "income": 0.08,
                     "momentum": 0.03, "preference_fit": 0.12, "diversification": 0.10, "news_events": 0.07},
    "balanced": {"risk_fit": 0.20, "loss_fit": 0.15, "return_quality": 0.10, "fundamentals": 0.10, "income": 0.05,
                 "momentum": 0.05, "preference_fit": 0.15, "diversification": 0.12, "news_events": 0.08},
    "growth": {"risk_fit": 0.14, "loss_fit": 0.10, "return_quality": 0.14, "fundamentals": 0.10, "income": 0.02,
               "momentum": 0.06, "preference_fit": 0.18, "diversification": 0.18, "news_events": 0.08},
}
STRESS_TOLERANCE = {"low": 0.08, "low_moderate": 0.12, "moderate": 0.20, "high": 0.45}   # calendar-2022 loss
DATA_YEARS = 5


def _horizon_class(years: int) -> str:
    return "short" if years <= 5 else "medium" if years <= 12 else "long"


drawdown_tolerance = profile_drawdown_tolerance


def compute_weights(objective: str, horizon_class: str, needs: dict) -> tuple[dict[str, float], list[str]]:
    w = dict(BASE_WEIGHTS[objective])
    reasons = [f"Base weights for a {objective} objective."]

    def scale(key: str, factor: float, why: str) -> None:
        w[key] *= factor
        reasons.append(f"{COMPONENTS[key]} x{factor:g}: {why}.")

    if horizon_class == "short":
        scale("loss_fit", 1.3, "a short horizon leaves little time to recover from a fall")
        scale("momentum", 1.2, "recent trend matters more over a few years")
        scale("fundamentals", 0.8, "business quality pays off mostly over longer periods")
        scale("return_quality", 0.8, "past risk-adjusted returns are a weak guide over a short horizon")
    elif horizon_class == "long":
        scale("fundamentals", 1.3, "business quality matters most over long periods")
        scale("return_quality", 1.2, "a long horizon can wait through volatility")
        scale("momentum", 0.6, "short-term trends matter little over 12+ years")
        scale("loss_fit", 0.9, "more time to recover from falls")
    if needs.get("income"):
        scale("income", 1.6, "you listed income or dividends as an interest")
    if needs.get("quality"):
        scale("fundamentals", 1.3, "you prefer established, understandable companies")
    if needs.get("esg") != "none":
        scale("news_events", 1.4, "controversy evidence in the news matters for your ESG preferences")
        scale("preference_fit", 1.1, "your ESG preferences are part of the fit")
    total = sum(w.values())
    return {k: round(v / total, 4) for k, v in w.items()}, reasons


def investor_features(profile: dict, provider=None) -> dict:
    """Classifications and tolerances derived from the profile (and its holdings, if any)."""
    rl = RISK_LEVELS[profile["risk_level"]]
    hz = _horizon_class(profile["horizon_years"])
    objective = OBJECTIVE_BY_RISK[profile["risk_level"]]
    interests = set(profile.get("interests") or [])
    text = (profile.get("objective") or "").lower()
    esg = "strict" if profile.get("esg_exclusions") and "esg" in interests else \
        "screen" if profile.get("esg_exclusions") or "esg" in interests else "none"
    aversion = {"low": "high", "low_moderate": "high", "moderate": "medium", "high": "low"}[profile["risk_level"]]
    if aversion == "medium" and ("speculat" in text or profile.get("crypto") == "avoid"):
        aversion = "high"
    needs = {
        "income": "dividend" in interests or "income" in text,
        "quality": "established" in interests or "understand" in text,
        "esg": esg,
        "growth": "high" if objective == "growth" or {"growth", "ai", "emerging_tech"} & interests else
                  "some" if objective == "balanced" or "growth" in text else "low",
        "stability": objective == "preservation" or "stabil" in text or "preserve" in text,
    }
    dd_tol, dd_why = drawdown_tolerance(profile)
    weights, reasons = compute_weights(objective, hz, needs)
    caps = effective_caps(profile)
    lo, hi = rl["vol_band"]
    portfolio = {}
    if provider is not None and profile.get("holdings"):
        hw = weights_from_holdings(profile["holdings"])
        assets = provider.assets
        portfolio = {
            "holdings": len(hw),
            "tech_growth_share": round(sum(w for t, w in hw.items() if {"tech", "growth"} & set(assets[t]["tags"])), 4),
            "crypto_share": round(sum(w for t, w in hw.items() if assets[t]["asset_group"] == "crypto"), 4),
            "defensive_share": round(sum(w for t, w in hw.items() if assets[t]["is_defensive"]), 4),
            "largest": max(hw.items(), key=lambda kv: kv[1])[0] if hw else None,
        }
    label = {"low": "Low", "medium": "Medium", "high": "High", "some": "Some"}
    classes = [
        {"key": "risk_capacity", "label": "Risk capacity", "value": rl["label"],
         "why": f"Risk level in the profile; single-holding volatility band {lo:.0%} to {hi:.0%} a year."},
        {"key": "horizon", "label": "Horizon", "value": f"{hz.capitalize()} ({profile['horizon_years']} years)",
         "why": "Short is 5 years or less, medium 6 to 12, long more than 12."},
        {"key": "objective", "label": "Objective", "value": objective.capitalize(),
         "why": "Implied by the risk level: low and low-to-moderate preserve capital, moderate balances, high grows."},
        {"key": "loss_tolerance", "label": "Loss tolerance", "value": f"{dd_tol:.0%} peak to trough",
         "why": f"The lower of the risk-level and horizon limits; here set by the {dd_why}."},
        {"key": "stress_tolerance", "label": "2022-type stress tolerance", "value": f"{STRESS_TOLERANCE[profile['risk_level']]:.0%} loss",
         "why": "Largest calendar-2022 loss treated as acceptable for this risk level."},
        {"key": "recovery_budget", "label": "Recovery time budget", "value": f"{round(profile['horizon_years'] * 252 * 0.25)} trading days",
         "why": "A quarter of the horizon: a fall should be recovered well before the money is needed."},
        {"key": "income_need", "label": "Income need", "value": "Yes" if needs["income"] else "No",
         "why": "Dividends listed as an interest." if needs["income"] else "No income interest in the profile."},
        {"key": "quality_focus", "label": "Quality focus", "value": "Yes" if needs["quality"] else "No",
         "why": "Prefers established companies." if needs["quality"] else "No preference for established names stated."},
        {"key": "esg", "label": "ESG sensitivity", "value": esg.capitalize(),
         "why": "Exclusion screens plus ESG interest = strict; either one = screen."},
        {"key": "speculation_aversion", "label": "Speculation aversion", "value": label[aversion],
         "why": "From the risk level, raised one step if the objective mentions speculation or crypto is avoided."},
        {"key": "growth_appetite", "label": "Growth appetite", "value": label[needs["growth"]],
         "why": "From the objective and growth, AI or emerging-tech interests."},
    ]
    return {
        "risk_level": profile["risk_level"], "risk_label": rl["label"], "vol_band": [lo, hi],
        "horizon_years": profile["horizon_years"], "horizon_class": hz, "objective": objective,
        "drawdown_tolerance": dd_tol, "drawdown_tolerance_source": dd_why,
        "stress_tolerance": STRESS_TOLERANCE[profile["risk_level"]],
        "recovery_budget_days": round(profile["horizon_years"] * 252 * 0.25),
        "needs": needs, "speculation_aversion": aversion, "themes": sorted(interests),
        "crypto": profile.get("crypto"), "esg_exclusions": profile.get("esg_exclusions") or [],
        "caps": caps, "portfolio": portfolio,
        "weights": weights, "weight_reasons": reasons,
        "horizon_beyond_data": profile["horizon_years"] > DATA_YEARS,
        "classifications": classes,
    }
