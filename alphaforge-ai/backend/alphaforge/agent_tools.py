"""Tools the advisor agent can call. One definition serves both the AgentCore
Gateway Lambda target (AWS) and local tests.

Every tool takes a `context_key`. The API creates one random key per question
and binds it to the authenticated user (table agent_sessions, 15-minute TTL),
so the model cannot choose whose profile is read. Each call is logged under the
key; that log is the observable trace shown to the user and the reference for
checking the numbers in the agent's answer.

    python -m alphaforge.agent_tools > ../agents/gateway_tools.json   # inline tool schema for the Gateway target
"""

from __future__ import annotations

import json
import sys

from .data.provider import DataProvider
from .governance import check_proposal
from .portfolio import stress_test, weights_from_holdings
from .profile_features import investor_features
from .profiles import effective_caps, profile_assumptions
from .radar import compute_radar
from .scoring import ScoringContext, score_asset, score_universe
from .suggestions import build_suggestions

MAX_RESULT_CHARS = 20_000


class ToolInputError(ValueError):
    pass


def _tickers(raw, provider: DataProvider) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    tickers = [str(t).strip().upper() for t in items if str(t).strip()]
    unknown = [t for t in tickers if t not in provider.assets]
    if unknown:
        raise ToolInputError(f"unknown tickers {unknown}; known tickers: {sorted(provider.assets)}")
    return tickers


def _one_ticker(args, provider) -> str:
    t = _tickers(args.get("ticker"), provider)
    if len(t) != 1:
        raise ToolInputError("give exactly one ticker")
    return t[0]


def _compact_score(r: dict) -> dict:
    return {
        "ticker": r["ticker"], "name": r["name"], "score": r["score"], "verdict": r["verdict"], "status": r["status"],
        "badges": r["badges"], "gates": r["gates"], "current_weight": r["current_weight"], "metrics": r["metrics"],
        "key_features": r["key_features"],
        "components": [{"key": c["key"], "label": c["label"], "score": c["score"], "weight": c["weight"],
                        "applicable": c["applicable"], "explanation": c["explanation"],
                        "evidence": [{k: e.get(k) for k in ("claim", "value", "unit", "source_id", "period")} for e in c["evidence"][:3]]}
                       for c in r["components"]],
        "drivers": r["drivers"], "confidence": r["confidence"], "period": r["period"], "features_as_of": r["features_as_of"],
    }


def _signal(s: dict) -> dict:
    return {k: s.get(k) for k in ("published_at", "title", "publisher", "source_url", "source_tier", "kind", "event_type",
                                  "materiality", "sentiment", "esg_category", "linked_move") if s.get(k) is not None}


def get_profile_summary(args, profile, provider):
    return {"profile": {k: profile[k] for k in ("display_name", "risk_level", "horizon_years", "objective",
                                                "interests", "esg_exclusions", "crypto")},
            "holdings_weights": {t: round(w, 4) for t, w in weights_from_holdings(profile["holdings"]).items()},
            "caps": effective_caps(profile), "assumptions": profile_assumptions(profile)}


def get_profile_features(args, profile, provider):
    inv = investor_features(profile, provider)
    return {k: inv[k] for k in ("risk_label", "vol_band", "horizon_years", "horizon_class", "objective", "drawdown_tolerance",
                                "drawdown_tolerance_source", "stress_tolerance", "recovery_budget_days", "needs",
                                "speculation_aversion", "themes", "crypto", "esg_exclusions", "portfolio", "weights",
                                "weight_reasons", "horizon_beyond_data", "classifications")}


GROUPS_DEFAULT = ("returns", "risk", "risk_adjusted", "technical", "size_income", "fundamentals", "news")


def get_asset_features(args, profile, provider):
    if not provider.has_features:
        raise ToolInputError("asset_features.json is not loaded; use get_asset_metrics")
    groups = [g.strip() for g in str(args.get("groups") or ",".join(GROUPS_DEFAULT)).split(",") if g.strip()]
    defs = provider.feature_definitions()
    out = {}
    for t in _tickers(args.get("tickers"), provider):
        vals = provider.features(t)
        miss = provider.feature_missing(t)
        sel = {k: {"value": v, "label": defs[k]["label"], "unit": defs[k]["unit"], "window": defs[k]["window"]}
               for k, v in vals.items() if k in defs and defs[k]["group"] in groups}
        out[t] = {"features": sel, "missing": {k: r for k, r in miss.items() if k in defs and defs[k]["group"] in groups},
                  "source_id": f"asset_features.json#assets.{t}.values"}
    return {"as_of": provider.features_as_of, "data_through": provider.data_through, "base_currency": "CAD",
            "groups": {g: provider.feature_groups().get(g) for g in groups}, "assets": out,
            "unavailable": provider.features_unavailable()}


def get_news(args, profile, provider):
    from datetime import date, timedelta

    end = str(args.get("end") or provider.features_as_of or date.today().isoformat())[:10]
    days = max(1, min(int(args.get("days") or 30), 2000))
    start = str(args.get("start") or (date.fromisoformat(end) - timedelta(days=days)).isoformat())[:10]
    tickers = _tickers(args.get("tickers"), provider) if args.get("tickers") else None
    kinds = tuple(k.strip() for k in str(args.get("kinds") or "news,filing,macro,official").split(","))
    items = provider.signals_between(tickers, start, end, kinds) if tickers else \
        [s for s in provider.signals_between(None, start, end, kinds)]
    material = [s for s in items if s.get("materiality") == "high"]
    news = [s for s in items if s.get("kind") == "news"]
    return {"window": f"{start} to {end}", "count": len(items), "articles": len(news), "high_materiality": len(material),
            "avg_sentiment": None if not news else round(sum(float(s.get("sentiment") or 0) for s in news) / len(news), 3),
            "items": [_signal(s) for s in (material + [s for s in items if s not in material])[:25]],
            "note": "Sources: SEC EDGAR filings, Federal Reserve and Bank of Canada releases, policy-rate decisions from "
                    "the macro series, and headlines from allow-listed outlets only."}


def explain_price_move(args, profile, provider):
    from datetime import date, timedelta

    t = _one_ticker(args, provider)
    det = provider.feature_details(t)
    moves = det.get("big_moves") or []
    start, end = str(args.get("start") or "2000-01-01")[:10], str(args.get("end") or "2100-01-01")[:10]
    picked = [m for m in moves if start <= m["date"] <= end] or (sorted(moves, key=lambda m: -abs(m["z"]))[:3] if not args.get("start") else [])
    out = []
    for m in picked[:6]:
        d = date.fromisoformat(m["date"])
        win = provider.signals_between([t], (d - timedelta(days=2)).isoformat(), (d + timedelta(days=1)).isoformat())
        mac = [s for s in provider.macro_signals((d - timedelta(days=3)).isoformat(), (d + timedelta(days=1)).isoformat())
               if s.get("kind") == "macro"]
        out.append({**m, "news_and_filings": [_signal(s) for s in win[:4]], "policy_decisions": [_signal(s) for s in mac[:2]]})
    return {"ticker": t, "moves": out, "drawdown_5y": det.get("drawdown_5y"),
            "stress_2022": provider.feature(t, "stress_2022"),
            "method": "Unusual moves are daily returns at least 3 times the trailing one-year standard deviation. News and "
                      "filings are looked up from 2 days before to 1 day after; they are context, not proof of cause."}


def get_market_regime(args, profile, provider):
    r = provider.market_regime()
    if not r:
        raise ToolInputError("market regime needs asset_features.json")
    return r


def get_asset_metrics(args, profile, provider):
    window, note = provider.resolve_window(args.get("window"))
    out = {}
    for t in _tickers(args.get("tickers"), provider):
        a = provider.assets[t]
        out[t] = {"metadata": {k: a[k] for k in ("name", "asset_class", "region", "listing_currency", "sector", "tags")},
                  "metrics": provider.metrics(t, window),
                  "source_id": f"asset_metrics.json#windows.{window}.metrics.{t}"}
    return {"window": window, "period": provider.window_period(window), "window_note": note,
            "provenance": provider.metrics_provenance, "assets": out}


def score_assets(args, profile, provider):
    res = score_universe(profile, provider, args.get("window"))
    include = str(args.get("include_excluded", False)).lower() == "true"
    rows = [r for r in res["results"] if include or r["status"] != "excluded"]
    top_n = max(1, min(int(args.get("top_n") or 10), len(rows) or 1))
    return {"window": res["window"], "period": res["period"], "features_as_of": res["features_as_of"],
            "weights": res["component_weights"], "assumptions": res["assumptions"], "sensitivity": res["sensitivity"]["method"],
            "results": [{"rank": r["rank"], "ticker": r["ticker"], "score": r["score"], "verdict": r["verdict"]["label"],
                         "status": r["status"], "badges": r["badges"], "rank_stability": r["rank_stability"],
                         "top_driver": (r["drivers"]["up"] or [{}])[0].get("label"),
                         "main_drag": (r["drivers"]["down"] or [{}])[0].get("label")}
                        for r in rows[:top_n]]}


def explain_asset_fit(args, profile, provider):
    ctx = ScoringContext(profile, provider, args.get("window"))
    return _compact_score(score_asset(_one_ticker(args, provider), ctx))


def check_constraints(args, profile, provider):
    try:
        weight = float(args.get("weight"))
    except (TypeError, ValueError):
        raise ToolInputError("weight must be a decimal between 0 and 1 (0.3 = 30%)") from None
    res = check_proposal(profile, {"type": "add", "ticker": _one_ticker(args, provider), "weight": weight},
                         provider, args.get("window"))
    fit = res.pop("asset_fit", None)
    if fit:
        res["asset_fit"] = {k: fit[k] for k in ("ticker", "score", "status", "badges")}
    return res


def get_portfolio_radar(args, profile, provider):
    r = compute_radar(profile, provider, args.get("window"))
    return {"period": r["period"], "stats": r["stats"], "limitations": r["limitations"], "assumptions": r["assumptions"],
            "axes": [{k: a[k] for k in ("key", "label", "value", "target", "gap", "explanation")} for a in r["axes"]]}


def get_suggestions(args, profile, provider):
    s = build_suggestions(profile, provider, args.get("window"))
    return {"suggestions": [{k: it[k] for k in ("title", "severity", "detail", "candidates")} for it in s["items"]],
            "top_picks": s["top_picks"], "notes": s["notes"]}


def run_stress(args, profile, provider):
    window, _ = provider.resolve_window(args.get("window"))
    return stress_test(weights_from_holdings(profile["holdings"]), provider, window,
                       args.get("scenario") or "2022_rates_shock")


def get_signals(args, profile, provider):
    tickers = _tickers(args.get("tickers"), provider) if args.get("tickers") else None
    days = max(1, min(int(args.get("days") or 14), 90))
    return {"days": days, "signals": provider.signals(tickers, days=days)[:20],
            "note": None if provider.signals() else "No signals ingested yet."}


_CTX = {"context_key": {"type": "string", "description": "Session key given in the user message. Pass it unchanged."}}
_WINDOW = {"window": {"type": "string", "description": "Metrics window such as '3y' or '5y'. Omit for the default."}}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {**_CTX, **props}, "required": ["context_key", *required]}


TOOLS = {
    "get_profile_summary": (get_profile_summary,
        "The user's fictional investor profile, current weights, caps and the assumptions behind them.",
        _schema({}, [])),
    "get_profile_features": (get_profile_features,
        "How the app reads the profile: risk capacity, horizon class, loss and stress tolerances, needs (income, quality, "
        "ESG), speculation aversion and the component weights of the fit score with the reason for each adjustment.",
        _schema({}, [])),
    "get_asset_features": (get_asset_features,
        "Computed features per asset (CAD base): returns 1M-3Y, volatility, drawdowns, Sharpe/Sortino, beta, RSI, moving "
        "averages, MACD, ATR, liquidity, market cap, dividends, fundamentals (SEC XBRL), factor exposures, GARCH forecast, "
        "regime probability, news and insider features, with definitions and reasons for missing values.",
        _schema({"tickers": {"type": "string", "description": "Comma-separated tickers"},
                 "groups": {"type": "string", "description": "Comma-separated groups: returns, risk, tail, risk_adjusted, relative, "
                                                             "technical, liquidity, size_income, fundamentals, factors, forecast, "
                                                             "regime, news, insider, earnings (default: the main ones)"}}, ["tickers"])),
    "get_news": (get_news,
        "Dated filings (SEC EDGAR), central-bank releases, policy-rate decisions and headlines from allow-listed outlets, "
        "with publisher, link, event type, materiality and sentiment. Use it to explain answers with sources.",
        _schema({"tickers": {"type": "string", "description": "Comma-separated tickers; omit for market-wide items"},
                 "days": {"type": "integer", "description": "Look-back in days from the end date (default 30)"},
                 "start": {"type": "string", "description": "Start date YYYY-MM-DD (overrides days)"},
                 "end": {"type": "string", "description": "End date YYYY-MM-DD (default: data as-of date)"},
                 "kinds": {"type": "string", "description": "Comma-separated: news, filing, macro, official"}}, [])),
    "explain_price_move": (explain_price_move,
        "Unusual daily moves of one asset (3+ standard deviations) with the filings, articles and policy decisions published "
        "around each date. Use for 'why did X fall/rise'.",
        _schema({"ticker": {"type": "string", "description": "One ticker"},
                 "start": {"type": "string", "description": "Start date YYYY-MM-DD (optional)"},
                 "end": {"type": "string", "description": "End date YYYY-MM-DD (optional)"}}, ["ticker"])),
    "get_market_regime": (get_market_regime,
        "Current market regime (stress, correction, volatile, calm uptrend, mixed) from SPY and XIU.TO trend, drawdown and "
        "regime probability, plus policy rates and yield curves, with the rules used.",
        _schema({}, [])),
    "get_asset_metrics": (get_asset_metrics,
        "Historical risk/return metrics (CAD base) for assets, with the data period and provenance. Use for any number you state about an asset.",
        _schema({"tickers": {"type": "string", "description": "Comma-separated tickers, e.g. 'TSLA,SPY'"}, **_WINDOW}, ["tickers"])),
    "score_assets": (score_assets,
        "Profile-fit ranking (0-100) of the asset universe, best first, with verdict, status (ok, caution, cap_reached, "
        "over_cap, excluded), rank stability under perturbed weights, top driver and main drag.",
        _schema({"top_n": {"type": "integer", "description": "Number of results (default 10)"},
                 "include_excluded": {"type": "boolean", "description": "Include assets excluded by the user's preferences"}, **_WINDOW}, [])),
    "explain_asset_fit": (explain_asset_fit,
        "Breakdown of one asset's fit score: each component's score, weight, explanation and evidence, gates and drivers.",
        _schema({"ticker": {"type": "string", "description": "One ticker"}, **_WINDOW}, ["ticker"])),
    "check_constraints": (check_constraints,
        "Governance check of a proposed fictional position against the user's caps, ESG screens, risk limits and horizon. "
        "Returns status (aligned, review, conflict), flags, before/after statistics and alternatives. Call it for ANY request to add or increase a position.",
        _schema({"ticker": {"type": "string", "description": "Ticker to add"},
                 "weight": {"type": "number", "description": "Share of the portfolio as a decimal, 0.3 = 30%"}, **_WINDOW}, ["ticker", "weight"])),
    "get_portfolio_radar": (get_portfolio_radar,
        "Nine-axis portfolio profile (diversification, risk, geography, USD exposure, defensiveness, growth, ESG, income, event exposure) vs the target for the profile.",
        _schema({**_WINDOW}, [])),
    "get_suggestions": (get_suggestions,
        "Rule-based suggestions from portfolio gaps and constraint flags, with candidate assets and what-if impacts, plus top picks.",
        _schema({**_WINDOW}, [])),
    "run_stress": (run_stress,
        "Replay a historical scenario on the user's fictional portfolio; reports coverage and holdings without data.",
        _schema({"scenario": {"type": "string", "description": "Scenario id, default '2022_rates_shock'"}, **_WINDOW}, [])),
    "get_signals": (get_signals,
        "Recent enriched signals (world events, official statements, filings, posts) touching tickers, with source URLs, severity and sentiment.",
        _schema({"tickers": {"type": "string", "description": "Comma-separated tickers; omit for all"},
                 "days": {"type": "integer", "description": "Look-back in days (default 14, max 90)"}}, [])),
}


def run_tool(name: str, args: dict, profile: dict, provider: DataProvider) -> dict:
    if name not in TOOLS:
        raise ToolInputError(f"unknown tool {name!r}")
    return TOOLS[name][0](args, profile, provider)


def execute_session_tool(name: str, args: dict, store, provider: DataProvider) -> tuple[str, str]:
    """Run one agent tool call bound to a session key and log it under that key.

    Shared by the Gateway Lambda target and the inline-function loop in ask.py. The
    profile comes from the session's user, never from the model's arguments.
    Returns (status, result_json_text).
    """
    import logging
    from datetime import datetime, timezone

    args = dict(args or {})
    key = str(args.pop("context_key", "") or "")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = store.get("agent_sessions", key) if key else None
    try:
        if not session:
            raise ToolInputError("invalid or expired context_key; use the key given in the user message")
        profile = store.get("profiles", session["user_id"])
        if not profile:
            raise ToolInputError("the user has no saved profile")
        result, status = run_tool(name, args, profile, provider), "ok"
    except (ToolInputError, ValueError) as e:
        result, status = {"error": str(e)}, "input_error"
    except Exception:  # noqa: BLE001
        logging.getLogger("alphaforge.tools").exception("tool %s failed", name)
        result, status = {"error": "tool failed"}, "error"
    text = truncate(result)
    if session:
        # Server-side trace of observable actions, read back by the API after the agent answers.
        store.append("agent_sessions", key, "calls",
                     {"at": started, "tool": name, "input": args, "status": status, "output": text})
    return status, text


def inline_tool_specs() -> list[dict]:
    """The tools as AgentCore Harness inline functions (passed on each InvokeHarness call)."""
    return [{"type": "inline_function", "name": n,
             "config": {"inlineFunction": {"description": d, "inputSchema": s}}} for n, (_, d, s) in TOOLS.items()]


def gateway_tool_schema() -> list[dict]:
    """Inline tool schema for create-gateway-target (mcp.lambda.toolSchema.inlinePayload)."""
    return [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]


def truncate(result: dict) -> str:
    text = json.dumps(result, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + '..."(truncated)"'


if __name__ == "__main__":
    json.dump(gateway_tool_schema(), sys.stdout, indent=1)
    sys.stdout.write("\n")
