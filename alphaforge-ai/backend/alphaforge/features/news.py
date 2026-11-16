"""Per-asset news, filing and insider features from signals.json and fundamentals.json.

Signals are written by the ingest jobs (alphaforge.ingest). Every feature here uses only
signals published on or before the as-of date, so a backtest cannot see later news.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone

HALF_LIFE_DAYS = 14
CONTROVERSY_LOOKBACK_DAYS = 365


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _as_of_dt(as_of: str) -> datetime:
    return datetime.combine(date.fromisoformat(as_of), datetime.max.time()).replace(tzinfo=timezone.utc)


def by_ticker(signals: list[dict], as_of: str) -> dict[str, list[dict]]:
    cutoff = _as_of_dt(as_of)
    out: dict[str, list[dict]] = {}
    for s in signals:
        t = _ts(s.get("published_at"))
        if t is None or t > cutoff:
            continue
        for tk in s.get("tickers") or []:
            out.setdefault(tk, []).append(s)
    for v in out.values():
        v.sort(key=lambda s: s["published_at"], reverse=True)
    return out


def news_features(items: list[dict], as_of: str) -> tuple[dict, dict, dict]:
    """Return (features, missing reasons, details) for one asset's signals."""
    now = _as_of_dt(as_of)

    def age(s):
        return (now - _ts(s["published_at"])).total_seconds() / 86400

    media = [s for s in items if s.get("kind") == "news"]
    filings = [s for s in items if s.get("kind") == "filing"]
    m30 = [s for s in media if age(s) <= 30]
    m90 = [s for s in media if age(s) <= 90]
    out, missing, details = {}, {}, {}
    out["news_count_30d"] = len(m30)
    out["news_count_90d"] = len(m90)
    out["filings_90d"] = sum(1 for s in filings if age(s) <= 90)
    if m30:
        w = [0.5 ** (age(s) / HALF_LIFE_DAYS) for s in m30]
        out["news_sentiment_30d"] = sum(wi * float(s.get("sentiment") or 0) for wi, s in zip(w, m30)) / sum(w)
    else:
        missing["news_sentiment_30d"] = "No articles from the allow-listed outlets in the last 30 days."
    recent = [float(s.get("sentiment") or 0) for s in m30]
    older = [float(s.get("sentiment") or 0) for s in media if 30 < age(s) <= 90]
    if len(recent) >= 2 and len(older) >= 2:
        out["news_sentiment_momentum"] = statistics.fmean(recent) - statistics.fmean(older)
    else:
        missing["news_sentiment_momentum"] = "Needs at least two articles in the last 30 days and two in days 31 to 90."
    material = [s for s in items if age(s) <= 90 and s.get("materiality") == "high"]
    out["material_events_90d"] = len(material)
    out["negative_material_90d"] = sum(1 for s in material if float(s.get("sentiment") or 0) < -0.15
                                       or s.get("event_type") in NEGATIVE_EVENT_TYPES)
    contro = [s for s in items if age(s) <= CONTROVERSY_LOOKBACK_DAYS and s.get("esg_category")
              and (float(s.get("sentiment") or 0) < 0 or s.get("event_type") in NEGATIVE_EVENT_TYPES)]
    high = [s for s in contro if s.get("materiality") == "high"]
    level = "significant" if high or len(contro) >= 3 else "minor" if contro else "none_found"
    out["esg_controversy_level"] = level
    out["esg_controversies_1y"] = len(contro)
    details["controversies"] = [_brief(s) for s in contro[:6]]
    details["latest"] = [_brief(s) for s in items[:8]]
    details["material"] = [_brief(s) for s in material[:6]]
    if items:
        out["last_news_date"] = items[0]["published_at"][:10]
    return out, missing, details


NEGATIVE_EVENT_TYPES = {"lawsuit", "regulatory_action", "investigation", "recall", "accident", "environmental_incident",
                        "bankruptcy", "delisting", "restatement", "layoffs", "impairment", "guidance_cut", "data_breach",
                        "labor_dispute", "sanctions"}


def _brief(s: dict) -> dict:
    return {k: s.get(k) for k in ("signal_id", "published_at", "title", "publisher", "source_url", "source_tier",
                                  "event_type", "esg_category", "sentiment", "materiality", "kind")}


def insider_features(insider: dict | None) -> tuple[dict, dict]:
    if not insider:
        return {}, {"insider_net_shares_90d": "Insider transactions come from SEC Form 4, filed only for U.S. issuers."}
    out = {k: insider.get(k) for k in ("insider_buys_90d", "insider_sells_90d", "insider_net_shares_90d",
                                       "insider_net_value_90d") if insider.get(k) is not None}
    return out, {}


def earnings_reactions(items: list[dict], own: dict[str, float], spy: dict[str, float]) -> tuple[dict, dict, list]:
    """Abnormal 2-day price reaction (asset minus SPY, listing currency) around earnings releases.

    Earnings release dates are 8-K filings with Item 2.02. The window runs from the close
    before the filing date to the close of the next trading day, so it covers releases made
    before the open and after the close. This is a proxy for an earnings surprise: true
    surprises need analyst consensus estimates, which are not available from a public source.
    """
    dates = sorted(own)
    spy_dates = sorted(spy)
    reactions = []
    for s in items:
        if s.get("kind") != "filing" or "2.02" not in (s.get("items") or []):
            continue
        d = s["published_at"][:10]
        i = next((k for k, x in enumerate(dates) if x >= d), None)
        if i is None or i == 0 or i + 1 >= len(dates):
            continue
        r = own[dates[i + 1]] / own[dates[i - 1]] - 1
        j = next((k for k, x in enumerate(spy_dates) if x >= d), None)
        if j is None or j == 0 or j + 1 >= len(spy_dates):
            continue
        m = spy[spy_dates[j + 1]] / spy[spy_dates[j - 1]] - 1
        reactions.append({"filing_date": d, "abnormal_return": round(r - m, 4), "return": round(r, 4),
                          "source_url": s.get("source_url")})
        if len(reactions) == 4:
            break
    if not reactions:
        return {}, {"earnings_reaction_last": "No 8-K Item 2.02 earnings release found (Canadian issuers furnish results "
                                              "on Form 6-K, which has no item codes)."}, []
    out = {"earnings_reaction_last": reactions[0]["abnormal_return"],
           "earnings_reaction_avg4": statistics.fmean(r["abnormal_return"] for r in reactions)}
    return out, {}, reactions


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def within(day: str, start: str, end: str) -> bool:
    return start <= day <= end


def window_dates(day: str, before: int, after: int) -> tuple[str, str]:
    d = date.fromisoformat(day)
    return (d - timedelta(days=before)).isoformat(), (d + timedelta(days=after)).isoformat()
