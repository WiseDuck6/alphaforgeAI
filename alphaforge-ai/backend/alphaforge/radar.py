"""Portfolio radar: nine 0-100 axes, the user's portfolio vs a target for the profile.

Axis functions take raw weights so suggestions can re-run them on a
"what if I add X" portfolio and show the before/after value.
"""

from __future__ import annotations

from .data.provider import DataProvider
from .portfolio import PortfolioStats, weights_from_holdings
from .profiles import RISK_LEVELS, radar_targets

RISK_SCALE = 0.40      # 100 on the risk axis = 40% annualised volatility
INCOME_SCALE = 0.05    # 100 on the income axis = 5% dividend yield
EVENT_SEVERITY = 4     # signals at or above this severity count as exposure
EVENT_DAYS = 14

AXES = [
    ("diversification", "Diversification", "Effective number of positions and how little they move together."),
    ("risk", "Risk level", "Estimated portfolio volatility; 100 = 40% a year."),
    ("geography", "Geographic spread", "Balance across Canada, U.S. and global assets (listing region)."),
    ("currency", "USD exposure", "Share of holdings listed in USD (currency risk for a CAD investor)."),
    ("defensiveness", "Defensiveness", "Share in bonds, gold, utilities, consumer staples and health care."),
    ("growth", "Growth / tech tilt", "Share in assets tagged growth or tech."),
    ("esg", "ESG alignment", "Share passing your ESG screens; unscreened index funds count half."),
    ("income", "Income", "Weighted dividend yield; 100 = 5% a year."),
    ("event_exposure", "Event exposure", "Share of holdings with recent high-severity signals; lower is calmer."),
]


def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def axis_values(weights: dict[str, float], profile: dict, provider: DataProvider, window: str) -> tuple[dict, PortfolioStats]:
    stats = PortfolioStats(weights, provider, window)
    assets = provider.assets
    period = provider.window_period(window)
    out: dict[str, dict] = {}

    # Diversification
    eff_n = stats.effective_n
    avg_c = stats.avg_pairwise_corr()
    avg_for_score = 1.0 if avg_c is None else max(avg_c, 0.0)
    out["diversification"] = {
        "value": _clip(50 * min(eff_n / 10, 1) + 50 * (1 - avg_for_score)),
        "explanation": f"Effective number of positions is {eff_n:.1f} (10 or more earns full marks on this half), and the "
                       f"weighted average pairwise correlation is {('n/a' if avg_c is None else f'{avg_c:.2f}')}.",
        "evidence": [{"claim": "Effective N (1/sum of squared weights)", "value": round(eff_n, 2), "source_id": "computed:holdings", "period": None},
                     {"claim": "Average pairwise correlation", "value": None if avg_c is None else round(avg_c, 3),
                      "source_id": "heuristic:asset_group_priors" if stats.heuristic_corr_used else "correlations.json", "period": period}],
    }

    # Risk
    vol = stats.volatility()
    out["risk"] = {
        "value": None if vol is None else _clip(vol / RISK_SCALE * 100),
        "explanation": "No volatility data." if vol is None else
        f"Estimated portfolio volatility {vol:.1%} a year from holding volatilities and correlations.",
        "evidence": [{"claim": "Estimated portfolio volatility", "value": None if vol is None else round(vol, 4),
                      "source_id": "computed:asset_metrics.ann_vol+correlations", "period": period}],
    }

    # Geography
    shares = {r: stats.share(lambda a, r=r: a["region"] == r) for r in ("CA", "US", "Global")}
    hhi = sum(s * s for s in shares.values())
    out["geography"] = {
        "value": _clip((1 - hhi) / (2 / 3) * 100),
        "explanation": "Listing-region mix: " + ", ".join(f"{r} {s:.0%}" for r, s in shares.items()) +
                       ". Broad funds are counted by listing region until holdings look-through is loaded.",
        "evidence": [{"claim": f"{r} share", "value": round(s, 4), "source_id": "assets.json#region", "period": None} for r, s in shares.items()],
    }

    # Currency
    usd = stats.share(lambda a: a["listing_currency"] == "USD")
    out["currency"] = {
        "value": _clip(usd * 100),
        "explanation": f"{usd:.0%} of the portfolio is listed in USD. Listing currency is a proxy: a CAD-listed fund can hold foreign assets.",
        "evidence": [{"claim": "USD-listed share", "value": round(usd, 4), "source_id": "assets.json#listing_currency", "period": None}],
    }

    # Defensiveness
    d = stats.share(lambda a: a["is_defensive"])
    out["defensiveness"] = {
        "value": _clip(d * 100),
        "explanation": f"{d:.0%} in defensive assets (bonds, gold, utilities, consumer staples, health care).",
        "evidence": [{"claim": "Defensive share", "value": round(d, 4), "source_id": "assets.json#is_defensive", "period": None}],
    }

    # Growth / tech
    g = stats.share(lambda a: bool({"growth", "tech"} & set(a["tags"])))
    out["growth"] = {
        "value": _clip(g * 100),
        "explanation": f"{g:.0%} in assets tagged growth or tech.",
        "evidence": [{"claim": "Growth/tech share", "value": round(g, 4), "source_id": "assets.json#tags", "period": None}],
    }

    # ESG
    screens = set(profile.get("esg_exclusions") or ["fossil_fuels"])
    esg_score, failing = 0.0, []
    for t, w in weights.items():
        flags = set(assets[t]["esg"]["flags"])
        if screens & flags:
            failing.append(t)
        elif "unscreened_index" in flags:
            esg_score += 0.5 * w
        else:
            esg_score += w
    note = "" if profile.get("esg_exclusions") else " No ESG screen is set in your profile; shown with a fossil-fuel screen for information only."
    out["esg"] = {
        "value": _clip(esg_score * 100),
        "explanation": (f"Holdings failing the screen: {', '.join(failing)}." if failing else "No holding fails the screen.") + note +
                       " Screens use sector-based flags, not third-party ESG ratings.",
        "evidence": [{"claim": "Holdings failing ESG screen", "value": failing, "source_id": "assets.json#esg.flags", "period": None}],
        "failing": failing,
    }

    # Income
    covered = total_yield = 0.0
    for t, w in weights.items():
        y = provider.metric(t, window, "dividend_yield_ttm")
        if y is not None:
            covered += w
            total_yield += w * y
    if covered == 0:
        out["income"] = {"value": None, "available": False, "evidence": [],
                         "explanation": "Dividend yields are not loaded yet (computed by data_pipeline from corporate_actions.csv)."}
    else:
        y = total_yield / covered
        out["income"] = {
            "value": _clip(y / INCOME_SCALE * 100),
            "explanation": f"Weighted trailing 12-month dividend yield {y:.2%} on {covered:.0%} of the portfolio with data.",
            "evidence": [{"claim": "Weighted TTM dividend yield", "value": round(y, 4), "source_id": "asset_metrics.json#dividend_yield_ttm", "period": period}],
        }

    # Event exposure
    exposed = []
    for t in weights:
        if any(s.get("severity", 0) >= EVENT_SEVERITY for s in provider.signals([t], days=EVENT_DAYS)):
            exposed.append(t)
    share_exposed = sum(weights[t] for t in exposed)
    out["event_exposure"] = {
        "value": _clip(share_exposed * 100),
        "explanation": (f"{share_exposed:.0%} of the portfolio has severity {EVENT_SEVERITY}+ signals in the last {EVENT_DAYS} days: {', '.join(exposed)}."
                        if exposed else f"No holding has severity {EVENT_SEVERITY}+ signals in the last {EVENT_DAYS} days"
                        + (" (no signals ingested yet)." if not provider.signals() else ".")),
        "evidence": [{"claim": "Exposed holdings", "value": exposed, "source_id": "signals", "period": f"last {EVENT_DAYS} days"}],
        "exposed": exposed,
    }
    return out, stats


def compute_radar(profile: dict, provider: DataProvider, window: str | None = None) -> dict:
    window, window_note = provider.resolve_window(window)
    weights = weights_from_holdings(profile["holdings"])
    targets = radar_targets(profile)
    rl = RISK_LEVELS[profile["risk_level"]]
    if not weights:
        return {
            "window": window, "period": provider.window_period(window), "window_note": window_note,
            "has_holdings": False,
            "axes": [{"key": k, "label": lbl, "description": desc, "value": None, "target": targets.get(k),
                      "gap": None, "available": False, "explanation": "Add holdings to see your portfolio shape.", "evidence": []}
                     for k, lbl, desc in AXES],
            "stats": None, "limitations": [], "assumptions": _assumptions(rl),
        }
    values, stats = axis_values(weights, profile, provider, window)
    axes = []
    for key, label, desc in AXES:
        v = values[key]
        val, tgt = v["value"], targets.get(key)
        axes.append({
            "key": key, "label": label, "description": desc,
            "value": None if val is None else round(val, 1),
            "target": tgt,
            "gap": None if (val is None or tgt is None) else round(val - tgt, 1),
            "available": v.get("available", val is not None),
            "explanation": v["explanation"],
            "evidence": v["evidence"],
        })
    vol = stats.volatility()
    rc = stats.risk_contributions()
    return {
        "window": window,
        "period": provider.window_period(window),
        "window_note": window_note,
        "has_holdings": True,
        "axes": axes,
        "stats": {
            "volatility": None if vol is None else round(vol, 4),
            "effective_n": round(stats.effective_n, 2),
            "avg_pairwise_corr": None if stats.avg_pairwise_corr() is None else round(stats.avg_pairwise_corr(), 3),
            "top3_share": round(stats.top_concentration(3), 4),
            "weights": [{"ticker": t, "name": provider.assets[t]["name"], "weight": round(w, 4)}
                        for t, w in sorted(weights.items(), key=lambda kv: -kv[1])],
            "risk_contributions": [{"ticker": t, "share_of_risk": round(s, 4)}
                                   for t, s in sorted(rc.items(), key=lambda kv: -kv[1])],
        },
        "limitations": stats.limitations() + ([window_note] if window_note else []),
        "assumptions": _assumptions(rl),
    }


def _assumptions(rl: dict) -> list[str]:
    return [
        f"Targets are team assumptions for a {rl['label'].lower()} profile, not recommendations.",
        f"Risk axis: 100 = {RISK_SCALE:.0%} annualised volatility; target = {rl['portfolio_vol_target']:.0%}.",
        "Regions and currencies use listing data; ETF holdings look-through is not loaded yet.",
    ]
