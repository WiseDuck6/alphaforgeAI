"""Governance checks: does a proposed (fictional) allocation respect the profile?

Severity levels:
    conflict  breaks a stated preference or a hard limit
    warning   allowed but in tension with the profile; must be explained
    info      missing information or context
The checker never blocks the user from exploring; it makes the tension visible.
Each flag has a `subject` (ticker, sector or "portfolio") and a numeric `value`
so a proposal can be compared with the current portfolio: a flag counts
against the proposal only if it is new or got worse.
"""

from __future__ import annotations

from . import SIMULATION_NOTICE
from .data.provider import DataProvider
from .portfolio import PortfolioStats, add_position, weights_from_holdings
from .profiles import (
    RISK_LEVELS,
    applicable_cap,
    effective_caps,
    horizon_label,
    profile_drawdown_tolerance,
)
from .scoring import ScoringContext, score_asset

TENSION_SCORE = 40  # fit score below which a high-profile request is called out
STEP = 0.005        # search step for the largest compatible position


class ProposalError(ValueError):
    pass


def _flag(code: str, severity: str, subject: str, value: float | None, message: str, evidence: list | None = None) -> dict:
    return {"code": code, "severity": severity, "subject": subject, "value": value,
            "message": message, "evidence": evidence or []}


def _summary(stats: PortfolioStats) -> dict:
    vol = stats.volatility()
    top = max(stats.weights.items(), key=lambda kv: kv[1]) if stats.weights else (None, 0)
    return {
        "volatility": None if vol is None else round(vol, 4),
        "effective_n": round(stats.effective_n, 2),
        "defensive_share": round(stats.share(lambda a: a["is_defensive"]), 4),
        "crypto_share": round(stats.share(lambda a: a["asset_group"] == "crypto"), 4),
        "largest_position": {"ticker": top[0], "weight": round(top[1], 4)},
    }


def evaluate_weights(weights: dict[str, float], profile: dict, provider: DataProvider, window: str) -> tuple[list[dict], PortfolioStats]:
    caps = effective_caps(profile)
    rl = RISK_LEVELS[profile["risk_level"]]
    tol = profile_drawdown_tolerance(profile)[0]
    stats = PortfolioStats(weights, provider, window)
    period = provider.window_period(window)
    flags: list[dict] = []
    assets = provider.assets

    for t, w in weights.items():
        a = assets[t]
        hits = set(profile["esg_exclusions"]) & set(a["esg"]["flags"])
        if hits:
            flags.append(_flag("esg_exclusion", "conflict", t, w,
                               f"{t} ({w:.0%}) fails your ESG screen: {', '.join(sorted(hits)).replace('_', ' ')}.",
                               [{"claim": "ESG flags", "value": a["esg"]["flags"], "source_id": f"assets.json#{t}.esg", "period": None}]))
        if a["sector"] in profile["avoid_sectors"]:
            flags.append(_flag("avoid_sector", "conflict", t, w, f"{t} is in {a['sector']}, which is on your avoid list."))
        if a["asset_group"] != "crypto":
            cap_name, cap = applicable_cap(a, caps)
            if w > cap + 0.0005:
                severity = "warning" if cap_name == "max_single_fund" else "conflict"
                flags.append(_flag("position_cap", severity, t, w,
                                   f"{t} would be {w:.1%} of the portfolio; the {cap_name.replace('_', ' ')} limit for this profile is {cap:.0%}."))

    crypto = stats.share(lambda a: a["asset_group"] == "crypto")
    if crypto > caps["max_crypto"] + 0.0005:
        flags.append(_flag("crypto_cap", "conflict", "portfolio", crypto,
                           f"Crypto would be {crypto:.1%} of the portfolio; the limit for this profile is {caps['max_crypto']:.0%}."))

    sectors: dict[str, float] = {}
    for t, w in weights.items():
        a = assets[t]
        if a["asset_group"] == "equity":
            sectors[a["sector"]] = sectors.get(a["sector"], 0) + w
    for sector, w in sectors.items():
        if w > caps["max_sector"] + 0.0005:
            flags.append(_flag("sector_concentration", "warning", sector, w,
                               f"Individual {sector} stocks would be {w:.0%} of the portfolio (limit {caps['max_sector']:.0%}). "
                               f"Funds are not looked through yet."))

    defensive = stats.share(lambda a: a["is_defensive"])
    if defensive + 0.0005 < caps["min_defensive"]:
        # value = shortfall so that "worse" means "bigger number"
        flags.append(_flag("defensive_floor", "warning", "portfolio", caps["min_defensive"] - defensive,
                           f"Defensive assets would be {defensive:.0%}; this profile uses a minimum of {caps['min_defensive']:.0%}."))

    vol = stats.volatility()
    if vol is not None:
        ev = [{"claim": "Estimated portfolio volatility", "value": round(vol, 4),
               "source_id": "computed:asset_metrics.ann_vol+correlations", "period": period}]
        if vol > rl["portfolio_vol_max"]:
            flags.append(_flag("portfolio_risk", "conflict", "portfolio", vol,
                               f"Estimated portfolio volatility {vol:.1%} is above the {rl['portfolio_vol_max']:.0%} limit used for a "
                               f"{rl['label'].lower()} profile.", ev))
        elif vol > rl["portfolio_vol_target"] * 1.25:
            flags.append(_flag("portfolio_risk", "warning", "portfolio", vol,
                               f"Estimated portfolio volatility {vol:.1%} is well above the {rl['portfolio_vol_target']:.0%} target.", ev))

    for t, w in weights.items():
        mdd = provider.metric(t, window, "max_drawdown")
        if mdd is not None and w >= 0.05 and abs(mdd) > tol:
            flags.append(_flag("horizon_drawdown", "warning", t, w,
                               f"{t} fell {abs(mdd):.0%} peak to trough in {period}. At {w:.0%} of the portfolio, a repeat would cost "
                               f"about {w * abs(mdd):.1%} of the total from this holding alone; the loss tolerance for this "
                               f"profile ({RISK_LEVELS[profile['risk_level']]['label'].lower()} risk, "
                               f"{horizon_label(profile['horizon_years'])} horizon) is {tol:.0%}.",
                               [{"claim": "Maximum drawdown", "value": mdd,
                                 "source_id": provider.evidence_ref(t, window, "max_drawdown"), "period": period}]))
    return flags, stats


def _mark_new(flags: list[dict], baseline: list[dict]) -> None:
    """A flag is 'new' if it did not exist before, or its value got worse."""
    base = {(f["code"], f["severity"], f["subject"]): f["value"] for f in baseline}
    for f in flags:
        key = (f["code"], f["severity"], f["subject"])
        f["new"] = key not in base or (f["value"] or 0) > (base[key] or 0) + 0.0005


def _status(flags: list[dict]) -> str:
    severities = {f["severity"] for f in flags}
    if "conflict" in severities:
        return "conflict"
    if "warning" in severities:
        return "review"
    return "aligned"


def parse_proposal(proposal: dict, current: dict[str, float], known: set[str]) -> tuple[dict[str, float], dict]:
    if not isinstance(proposal, dict):
        raise ProposalError("proposal must be a JSON object")
    kind = proposal.get("type")
    if kind == "add":
        t = str(proposal.get("ticker", "")).upper()
        if t not in known:
            raise ProposalError(f"unknown ticker {t!r}")
        try:
            w = float(proposal.get("weight"))
        except (TypeError, ValueError):
            raise ProposalError("weight must be a number between 0 and 1") from None
        if not 0 < w <= 1:
            raise ProposalError("weight must be between 0 and 1 (e.g. 0.3 for 30%)")
        return add_position(current, t, w), {"type": "add", "ticker": t, "weight": w}
    if kind == "allocation":
        cleaned = {}
        for t, w in (proposal.get("weights") or {}).items():
            t = str(t).upper()
            if t not in known:
                raise ProposalError(f"unknown ticker {t!r}")
            try:
                w = float(w)
            except (TypeError, ValueError):
                raise ProposalError(f"weight for {t} must be a number") from None
            if w > 0:
                cleaned[t] = w
        total = sum(cleaned.values())
        if total <= 0:
            raise ProposalError("allocation weights must sum to more than 0")
        return {t: w / total for t, w in cleaned.items()}, {"type": "allocation", "weights": cleaned}
    raise ProposalError("proposal.type must be 'add' or 'allocation'")


def check_proposal(profile: dict, proposal: dict, provider: DataProvider, window: str | None = None) -> dict:
    window, window_note = provider.resolve_window(window)
    current = weights_from_holdings(profile["holdings"])
    new_weights, normalized = parse_proposal(proposal, current, set(provider.assets))
    flags, after = evaluate_weights(new_weights, profile, provider, window)
    if current and normalized["type"] == "add":
        before_flags, before = evaluate_weights(current, profile, provider, window)
        _mark_new(flags, before_flags)
    else:
        before = PortfolioStats(current, provider, window) if current else None
        for f in flags:
            f["new"] = True
    if not current:
        flags.append({**_flag("no_holdings", "info", "portfolio", None,
                              "No current holdings in your profile, so the proposal is evaluated as the whole portfolio."),
                      "new": True})

    result = {
        "proposal": normalized,
        "window": window,
        "period": provider.window_period(window),
        "before": _summary(before) if before else None,
        "after": _summary(after),
        "notice": SIMULATION_NOTICE,
        "limitations": after.limitations() + ([window_note] if window_note else []),
    }

    if normalized["type"] == "add":
        t = normalized["ticker"]
        asset = provider.assets[t]
        ctx = ScoringContext(profile, provider, window)
        fit = score_asset(t, ctx)
        result["asset_fit"] = {"ticker": t, "name": asset["name"], "score": fit["score"], "status": fit["status"],
                               "badges": fit["badges"], "components": fit["components"], "confidence": fit["confidence"]}
        if "high_profile" in asset["tags"] and fit["score"] < TENSION_SCORE:
            flags.append({**_flag(
                "profile_tension", "warning", t, None,
                f"{asset['name']} is a widely followed, high-profile name, and its fit with this profile is {fit['score']}/100. "
                f"The request is in tension with the stated objective: {profile['objective'] or RISK_LEVELS[profile['risk_level']]['label']}. "
                f"Media attention is not evidence of suitability."), "new": True})
        result["alternatives"] = _alternatives(t, normalized["weight"], current, profile, provider, window, ctx)

    order = {"conflict": 0, "warning": 1, "info": 2}
    result["flags"] = sorted(flags, key=lambda f: (not f["new"], order[f["severity"]]))
    result["status"] = _status([f for f in flags if f["new"]])
    result["existing_issues"] = sum(1 for f in flags if not f["new"])
    return result


def _alternatives(t: str, requested: float, current: dict, profile: dict, provider: DataProvider, window: str,
                  ctx: ScoringContext) -> dict:
    """Largest position with no new or worse conflict, plus diversified funds sharing a theme."""
    baseline = evaluate_weights(current, profile, provider, window)[0] if current else []

    def ok(w: float) -> bool:
        flags, _ = evaluate_weights(add_position(current, t, w), profile, provider, window)
        _mark_new(flags, baseline)
        return not any(f["severity"] == "conflict" and f["new"] for f in flags)

    max_w = 0.0
    for i in range(int(round(requested / STEP)), 0, -1):
        if ok(i * STEP):
            max_w = i * STEP
            break
    warnings_at_max = []
    if max_w > 0:
        flags, _ = evaluate_weights(add_position(current, t, max_w), profile, provider, window)
        _mark_new(flags, baseline)
        warnings_at_max = [f["message"] for f in flags if f["severity"] == "warning" and f["new"]]

    themes = set(provider.assets[t]["tags"]) - {"broad_market", "high_profile", "established", "dividend"}
    funds = []
    for ticker, a in provider.assets.items():
        if ticker == t or a["asset_group"] != "broad_equity_fund":
            continue
        shared = themes & set(a["tags"])
        if shared:
            s = score_asset(ticker, ctx)
            if s["status"] != "excluded":
                funds.append({"ticker": ticker, "name": a["name"], "fit_score": s["score"], "shared_themes": sorted(shared)})
    funds.sort(key=lambda f: -f["fit_score"])
    return {
        "max_weight_without_new_conflicts": round(max_w, 4),
        "warnings_at_max_weight": warnings_at_max,
        "method": f"Largest position ({STEP:.1%} steps) that adds no new or worse conflict to the current portfolio.",
        "related_funds": funds[:2],
    }
