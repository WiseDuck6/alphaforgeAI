"""API routes. Importing this module registers them with the router."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import date, timedelta

from .. import SIMULATION_NOTICE, __version__
from ..ask import answer_agent, answer_local
from ..config import get_settings
from ..data.provider import get_provider
from ..governance import check_proposal
from ..monitoring.alerts import alerts_for
from ..monitoring.sources import get_scheduler, list_sources, update_source
from ..portfolio import stress_test, weights_from_holdings
from ..profile_features import COMPONENTS
from ..profiles import (
    CRYPTO_PREFS,
    ESG_EXCLUSION_OPTIONS,
    INTEREST_TAGS,
    PRESETS,
    RISK_LEVELS,
    effective_caps,
    preset_profile,
    profile_assumptions,
    validate_profile,
    with_risk_override,
)
from ..radar import compute_radar
from ..scoring import ScoringContext, score_asset, score_universe
from ..storage import get_store
from ..suggestions import build_suggestions
from .router import HttpError, Request, route

USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
log = logging.getLogger("alphaforge.api")


# ----- helpers --------------------------------------------------------------
def resolve_user(req: Request) -> str:
    if get_settings().auth_mode == "cognito":
        sub = req.claims.get("sub")
        if not sub:
            raise HttpError(401, "authentication required")
        return sub
    uid = req.headers.get("x-user-id") or "demo"
    if not USER_ID_RE.match(uid):
        raise HttpError(400, "invalid X-User-Id header")
    return uid


def load_profile(user_id: str) -> dict:
    store = get_store()
    profile = store.get("profiles", user_id)
    if profile is None and get_settings().auth_mode == "header" and user_id in PRESETS:
        # Dev convenience: demo users "amira", "mateo", "robert" start with their preset.
        profile = preset_profile(user_id, set(get_provider().assets))
        store.put("profiles", user_id, profile)
    if profile is None:
        raise HttpError(404, "no profile yet: create one in the profile wizard")
    return profile


def _profile_for_request(req: Request) -> tuple[str, dict, dict]:
    user = resolve_user(req)
    stored = load_profile(user)
    return user, stored, with_risk_override(stored, req.query.get("risk_level"))


# ----- meta -----------------------------------------------------------------
@route("GET", "/health")
def health(req: Request):
    provider = get_provider()
    return {"status": "ok", "version": __version__, "data_source": provider.source_label,
            "placeholder_data": provider.is_placeholder}


@route("GET", "/meta")
def meta(req: Request):
    provider = get_provider()
    return {
        "risk_levels": [{"key": k, "label": v["label"], "vol_band": v["vol_band"]} for k, v in RISK_LEVELS.items()],
        "interest_tags": INTEREST_TAGS,
        "esg_exclusions": ESG_EXCLUSION_OPTIONS,
        "crypto_prefs": CRYPTO_PREFS,
        "windows": {w: provider.window_period(w) for w in provider.windows},
        "default_window": provider.resolve_window(None)[0],
        "stress_scenarios": provider.stress_scenarios(),
        "assets": [{k: a[k] for k in ("ticker", "name", "asset_class", "asset_group", "region", "listing_currency", "sector")}
                   for a in provider.assets.values()],
        "components": COMPONENTS,
        "feature_definitions": provider.feature_definitions(),
        "feature_groups": provider.feature_groups(),
        "features_unavailable": provider.features_unavailable(),
        "features_as_of": provider.features_as_of,
        "data_through": provider.data_through,
        "notice": SIMULATION_NOTICE,
    }


@route("GET", "/presets")
def presets(req: Request):
    return [{"id": k, **{f: v.get(f) for f in ("display_name", "age", "risk_level", "horizon_years", "objective",
                                                  "interests", "esg_exclusions", "crypto", "brief_test", "demo_question")}}
            for k, v in PRESETS.items()]


# ----- profile --------------------------------------------------------------
@route("GET", "/me/profile")
def get_profile(req: Request):
    user = resolve_user(req)
    profile = load_profile(user)
    return {"user_id": user, "profile": profile, "caps": effective_caps(profile), "assumptions": profile_assumptions(profile)}


@route("PUT", "/me/profile")
def put_profile(req: Request):
    user = resolve_user(req)
    profile = validate_profile(req.body or {}, set(get_provider().assets))
    get_store().put("profiles", user, profile)
    return {"user_id": user, "profile": profile, "caps": effective_caps(profile)}


@route("POST", "/me/profile/preset/{preset_id}")
def load_preset(req: Request):
    user = resolve_user(req)
    profile = preset_profile(req.params["preset_id"], set(get_provider().assets))
    get_store().put("profiles", user, profile)
    return {"user_id": user, "profile": profile, "caps": effective_caps(profile)}


# ----- dashboard and analytics ----------------------------------------------
@route("GET", "/me/dashboard")
def dashboard(req: Request):
    user, stored, profile = _profile_for_request(req)
    provider = get_provider()
    window = req.query.get("window")
    scores = score_universe(profile, provider, window)
    radar = compute_radar(profile, provider, window)
    suggestions = build_suggestions(profile, provider, window, scores, radar)
    w = scores["window"]
    total = sum(h["amount"] for h in profile["holdings"])
    return {
        "user_id": user,
        "profile": stored,
        "what_if": {"risk_level": profile["risk_level"]} if profile is not stored else None,
        "portfolio_value": round(total, 2),
        "base_currency": "CAD",
        "window": w,
        "period": scores["period"],
        "window_note": scores["window_note"],
        "radar": radar,
        "suggestions": suggestions["items"],
        "suggestion_notes": suggestions["notes"],
        "top_picks": suggestions["top_picks"],
        "stress": stress_test(weights_from_holdings(profile["holdings"]), provider, w, "2022_rates_shock"),
        "alerts": alerts_for(profile, provider),
        "macro": provider.macro_snapshot(),
        "caps": effective_caps(profile),
        "assumptions": profile_assumptions(profile),
        "data_status": {"is_placeholder": provider.is_placeholder, "source": provider.source_label},
        "notice": SIMULATION_NOTICE,
    }


@route("GET", "/me/stress")
def stress(req: Request):
    _, _, profile = _profile_for_request(req)
    provider = get_provider()
    w, note = provider.resolve_window(req.query.get("window"))
    res = stress_test(weights_from_holdings(profile["holdings"]), provider, w, req.query.get("scenario", "2022_rates_shock"))
    res["window_note"] = note
    return res


@route("GET", "/assets")
def assets(req: Request):
    _, _, profile = _profile_for_request(req)
    return score_universe(profile, get_provider(), req.query.get("window"))


@route("GET", "/assets/{ticker}")
def asset_detail(req: Request):
    _, _, profile = _profile_for_request(req)
    provider = get_provider()
    ticker = req.params["ticker"].upper()
    if ticker not in provider.assets:
        raise HttpError(404, f"unknown ticker {ticker}")
    ctx = ScoringContext(profile, provider, req.query.get("window"))
    end = provider.features_as_of or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=int(req.query.get("news_days") or 365))).isoformat()
    return {
        "asset": provider.assets[ticker],
        "score": score_asset(ticker, ctx),
        "investor": ctx.inv,
        "window_note": ctx.window_note,
        "features": _feature_groups(provider, ticker),
        "feature_details": {k: v for k, v in provider.feature_details(ticker).items() if k != "news"},
        "features_as_of": provider.features_as_of,
        "data_through": provider.data_through,
        "signals": provider.signals_between([ticker], start, end),
        "price_history": provider.price_history(ticker),
        "caps": effective_caps(profile),
    }


def _feature_groups(provider, ticker: str) -> list[dict]:
    """Feature values grouped for display, with definitions and the reason for each missing value."""
    if not provider.has_features:
        return []
    defs, vals, miss = provider.feature_definitions(), provider.features(ticker), provider.feature_missing(ticker)
    groups: dict[str, list] = {}
    for key, d in defs.items():
        if key not in vals and key not in miss:
            continue
        groups.setdefault(d["group"], []).append({"key": key, **d, "value": vals.get(key), "missing_reason": miss.get(key),
                                                  "source_id": provider.feature_ref(ticker, key)})
    labels = provider.feature_groups()
    return [{"group": g, "label": labels.get(g, g), "features": items} for g, items in groups.items()]


@route("GET", "/market/regime")
def market_regime(req: Request):
    resolve_user(req)
    provider = get_provider()
    return {"regime": provider.market_regime(), "macro": provider.macro_snapshot(), "as_of": provider.features_as_of,
            "data_through": provider.data_through}


@route("GET", "/news")
def news(req: Request):
    resolve_user(req)
    provider = get_provider()
    end = req.query.get("end") or provider.features_as_of or date.today().isoformat()
    try:
        days = max(1, min(int(req.query.get("days") or 30), 2000))
        start = req.query.get("start") or (date.fromisoformat(end) - timedelta(days=days)).isoformat()
        date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HttpError(400, "start, end must be YYYY-MM-DD and days a whole number") from None
    tickers = [t.strip().upper() for t in (req.query.get("tickers") or "").split(",") if t.strip()] or None
    unknown = [t for t in tickers or [] if t not in provider.assets]
    if unknown:
        raise HttpError(400, f"unknown tickers {unknown}")
    kinds = tuple(k for k in (req.query.get("kinds") or "news,filing,macro,official").split(",") if k)
    items = provider.signals_between(tickers, start, end, kinds)
    return {"start": start, "end": end, "count": len(items), "items": items[:200]}


@route("GET", "/prices/sparklines")
def price_sparklines(req: Request):
    resolve_user(req)
    return get_provider().sparklines()


@route("POST", "/governance/check")
def governance_check(req: Request):
    _, _, profile = _profile_for_request(req)
    body = req.body or {}
    return check_proposal(profile, body.get("proposal"), get_provider(), body.get("window"))


# ----- ask (agent) ----------------------------------------------------------
JOB_TTL_SECONDS = 3600


@route("POST", "/ask")
def ask(req: Request):
    user, _, profile = _profile_for_request(req)
    body = req.body or {}
    question = str(body.get("question") or "").strip()
    if not question:
        raise HttpError(400, "question is required")
    if len(question) > 2000:
        raise HttpError(400, "question is too long (2000 characters max)")
    store = get_store()
    if get_settings().harness_arn and body.get("mode") != "local":
        # Agent answers can take longer than API Gateway's 30-second limit,
        # so they run as a background job that the browser polls.
        job_id = str(uuid.uuid4())
        store.put("ask_jobs", job_id, {"user_id": user, "question": question, "status": "pending",
                                       "risk_level": req.query.get("risk_level")}, ttl_seconds=JOB_TTL_SECONDS)
        _start_job(job_id)
        return {"job_id": job_id, "status": "pending"}
    result = answer_local(question, profile, get_provider(), body.get("window"))
    store.put("agent_traces", result["trace_id"], {"user_id": user, **result})
    return {"status": "done", **result}


@route("GET", "/ask/{job_id}")
def ask_job(req: Request):
    user = resolve_user(req)
    job = get_store().get("ask_jobs", req.params["job_id"])
    if not job or job.get("user_id") != user:
        raise HttpError(404, "job not found")
    if job["status"] == "done":
        return {"status": "done", "job_id": req.params["job_id"], **job["result"]}
    return {"status": job["status"], "job_id": req.params["job_id"], "error": job.get("error")}


def _start_job(job_id: str) -> None:
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if fn:
        import boto3

        boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event",
                                      Payload=json.dumps({"alphaforge_job": job_id}).encode())
    else:  # local dev server with a harness configured
        threading.Thread(target=run_ask_job, args=(job_id,), daemon=True).start()


def run_ask_job(job_id: str) -> None:
    store = get_store()
    job = store.get("ask_jobs", job_id)
    if not job or job["status"] != "pending":
        return
    try:
        profile = with_risk_override(load_profile(job["user_id"]), job.get("risk_level"))
        result = answer_agent(job["question"], job["user_id"], profile, get_provider(), get_settings(), store)
        store.put("agent_traces", result["trace_id"], {"user_id": job["user_id"], **result})
        store.put("ask_jobs", job_id, {**job, "status": "done", "result": result}, ttl_seconds=JOB_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        log.exception("ask job %s failed", job_id)
        store.put("ask_jobs", job_id, {**job, "status": "error", "error": "the agent could not answer; try again"},
                  ttl_seconds=JOB_TTL_SECONDS)


@route("GET", "/traces/{trace_id}")
def get_trace(req: Request):
    user = resolve_user(req)
    item = get_store().get("agent_traces", req.params["trace_id"])
    if not item or item.get("user_id") != user:
        raise HttpError(404, "trace not found")
    return item


# ----- monitoring -----------------------------------------------------------
@route("GET", "/sources")
def sources(req: Request):
    resolve_user(req)
    provider = get_provider()
    return {"sources": list_sources(get_store(), provider.ingest_status()),
            "signals_loaded": len(provider.signals()), "data_through": provider.data_through}


@route("PUT", "/sources/{source_id}")
def put_source(req: Request):
    user = resolve_user(req)
    if get_settings().auth_mode == "cognito":
        # Frequencies are global, so only members of the Cognito "admin" group may change them.
        groups = req.claims.get("cognito:groups") or ""
        if "admin" not in (groups if isinstance(groups, list) else groups.strip("[]").replace(",", " ").split()):
            raise HttpError(403, "only admins can change monitoring frequency")
    rate = (req.body or {}).get("rate")
    return update_source(get_store(), get_scheduler(), req.params["source_id"], rate, user)


@route("GET", "/alerts")
def alerts(req: Request):
    _, _, profile = _profile_for_request(req)
    return alerts_for(profile, get_provider())


@route("GET", "/data/status")
def data_status(req: Request):
    return get_provider().status()
