"""Monitoring sources and their adjustable scrape frequency.

Each source maps to one EventBridge Scheduler schedule named
ALPHAFORGE_SCHEDULER_PREFIX + source_id in group ALPHAFORGE_SCHEDULER_GROUP.
Changing the frequency in the UI calls UpdateSchedule on AWS; locally the
change is only recorded. The ingest Lambdas themselves are a later build step.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import get_settings

RATE_OPTIONS = {
    "off": None,
    "15m": "rate(15 minutes)",
    "1h": "rate(1 hour)",
    "6h": "rate(6 hours)",
    "1d": "rate(1 day)",
}

# Reliable sources only: regulators, central banks, the starter data's price source, and
# established news outlets (allow-listed in alphaforge.ingest.sources). Social media and
# forums are deliberately not ingested.
DEFAULT_SOURCES = [
    {
        "source_id": "market_data",
        "name": "Market data after the starter window",
        "category": "Prices and rates",
        "description": "Daily prices and dividends (Yahoo Finance chart API, the starter data's source), USD/CAD and Canadian "
                       "rates (Bank of Canada Valet), U.S. rates (FRED). Spliced onto the starter series, then features are rebuilt.",
        "url": "https://www.bankofcanada.ca/valet/docs",
        "access": "Free, no key",
        "default_rate": "1d",
    },
    {
        "source_id": "sec_edgar",
        "name": "SEC EDGAR filings, fundamentals and insider trades",
        "category": "Filings",
        "description": "8-K items and periodic reports (U.S. issuers and Canadian issuers cross-listed in the U.S.), annual XBRL "
                       "fundamentals and Form 4 insider transactions.",
        "url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "access": "Free; requires a descriptive User-Agent; fair-access rate limits",
        "default_rate": "6h",
    },
    {
        "source_id": "gdelt",
        "name": "News from allow-listed outlets",
        "category": "News",
        "description": "Headlines naming each company from Reuters, AP, Bloomberg, CNBC, WSJ, FT, NYT, The Globe and Mail, "
                       "Financial Post, CBC, BNN Bloomberg and others; found through GDELT or Google News RSS. Headline, "
                       "publisher, date and link only.",
        "url": "https://www.gdeltproject.org/",
        "access": "Free, no key (GDELT allows one request every 5 seconds)",
        "default_rate": "6h",
    },
    {
        "source_id": "official_rss",
        "name": "Central banks: releases and rate decisions",
        "category": "Policy",
        "description": "Federal Reserve and Bank of Canada press releases (RSS) and policy-rate decisions read from the rate series.",
        "url": "https://www.bankofcanada.ca/rss-feeds/",
        "access": "Free",
        "default_rate": "6h",
    },
    {
        "source_id": "news_backfill",
        "name": "News around past price moves (backfill)",
        "category": "News history",
        "description": "Articles published around each asset's largest daily moves since 2021, resumable in chunks. "
                       "Run until no moves remain, then switch off.",
        "url": "https://news.google.com/",
        "access": "Free, no key",
        "default_rate": "off",
    },
]
_DEFAULTS = {s["source_id"]: s for s in DEFAULT_SOURCES}


class SourceError(ValueError):
    pass


class LocalScheduler:
    def apply(self, source_id: str, expression: str | None) -> dict:
        return {"applied": False, "reason": "local mode: no EventBridge schedule was changed"}


class EventBridgeScheduler:
    """Updates an existing schedule. UpdateSchedule replaces the whole definition,
    so the current one is read first and copied with the new rate/state."""

    _COPY = ("Description", "ScheduleExpressionTimezone", "StartDate", "EndDate", "KmsKeyArn", "ActionAfterCompletion")

    def __init__(self, group: str, prefix: str):
        import boto3

        self._client = boto3.client("scheduler")
        self._group, self._prefix = group, prefix

    def apply(self, source_id: str, expression: str | None) -> dict:
        name = self._prefix + source_id
        current = self._client.get_schedule(Name=name, GroupName=self._group)
        params = {k: current[k] for k in self._COPY if current.get(k) is not None}
        params.update(
            Name=name,
            GroupName=self._group,
            ScheduleExpression=expression or current["ScheduleExpression"],
            FlexibleTimeWindow=current["FlexibleTimeWindow"],
            Target=current["Target"],
            State="DISABLED" if expression is None else "ENABLED",
        )
        self._client.update_schedule(**params)
        return {"applied": True, "schedule": name, "expression": params["ScheduleExpression"], "state": params["State"]}


def get_scheduler():
    s = get_settings()
    if s.scheduler_mode == "eventbridge":
        return EventBridgeScheduler(s.scheduler_group, s.scheduler_name_prefix)
    return LocalScheduler()


def list_sources(store, ingest_status: dict | None = None) -> list[dict]:
    """Sources with their frequency and last run. The last run comes from the store (written by
    the ingest Lambda) or, for local runs, from the bundle's ingest_status.json."""
    saved = {s["source_id"]: s for s in store.list("sources") if "source_id" in s}
    jobs = (ingest_status or {}).get("jobs") or {}
    out = []
    for sid, d in _DEFAULTS.items():
        cfg = saved.get(sid, {})
        job = jobs.get(sid) or {}
        last_run = cfg.get("last_run") or job.get("last_run")
        out.append({
            **d,
            "rate": cfg.get("rate", d["default_rate"]),
            "last_changed": cfg.get("changed_at"),
            "last_run": last_run,
            "last_status": cfg.get("last_status") or job.get("status") or "not run yet",
            "last_summary": job.get("summary") or cfg.get("last_report"),
            "rate_options": list(RATE_OPTIONS),
        })
    return out


def update_source(store, scheduler, source_id: str, rate: str, user_id: str) -> dict:
    if source_id not in _DEFAULTS:
        raise SourceError(f"unknown source {source_id!r}")
    if rate not in RATE_OPTIONS:
        raise SourceError(f"rate must be one of {list(RATE_OPTIONS)}")
    result = scheduler.apply(source_id, RATE_OPTIONS[rate])
    cfg = store.get("sources", source_id) or {"source_id": source_id}
    history = (cfg.get("history") or [])[-19:]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    history.append({"at": now, "rate": rate, "by": user_id, "scheduler": result})
    cfg.update(rate=rate, changed_at=now, history=history)
    store.put("sources", source_id, cfg)
    return {"source_id": source_id, "rate": rate, "scheduler": result}
