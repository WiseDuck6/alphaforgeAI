"""In-app alerts: signals that touch a user's holdings at or above their threshold."""

from __future__ import annotations

from ..data.provider import DataProvider

ALERT_LOOKBACK_DAYS = 14


def alerts_for(profile: dict, provider: DataProvider) -> dict:
    held = [h["ticker"] for h in profile["holdings"]]
    threshold = profile["settings"]["alert_threshold"]
    items = []
    for s in provider.signals(held, days=ALERT_LOOKBACK_DAYS):
        if s.get("severity", 0) >= threshold:
            items.append({
                "signal_id": s.get("signal_id"),
                "published_at": s.get("published_at"),
                "title": s.get("title") or s.get("summary"),
                "summary": s.get("summary"),
                "severity": s.get("severity"),
                "sentiment": s.get("sentiment"),
                "tickers": sorted(set(s.get("tickers", [])) & set(held)),
                "source_id": s.get("source_id"),
                "source_url": s.get("source_url"),
            })
    return {
        "threshold": threshold,
        "lookback_days": ALERT_LOOKBACK_DAYS,
        "items": items,
        "signals_loaded": len(provider.signals()),
        "note": None if provider.signals() else "No signals ingested yet: the monitoring pipeline is not connected.",
    }
