"""Lambda entry point for the ingest jobs (EventBridge Scheduler target).

Handler setting in Lambda: alphaforge.handlers.ingest_lambda.handler

Each Monitoring source has one schedule, alphaforge-ingest-<source_id>, whose input is
{"source_id": "<source_id>"}. The function runs that source's jobs against the S3 bundle
(ALPHAFORGE_DATA_SOURCE=s3://bucket/curated), rebuilds asset_features.json and records the
result on the source (table alphaforge-sources), which the Monitoring page shows.

Manual run: aws lambda invoke --function-name alphaforge-ingest --payload '{"jobs": ["news_backfill"]}' out.json
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import get_settings
from ..ingest.run import run
from ..storage import get_store

log = logging.getLogger()
log.setLevel(logging.INFO)

JOBS_BY_SOURCE = {
    "market_data": ["market_data"],
    "sec_edgar": ["sec_edgar"],
    "gdelt": ["gdelt"],
    "official_rss": ["official_rss"],
    "news_backfill": ["news_backfill"],
}


def handler(event, context):
    event = event or {}
    source = event.get("source_id")
    jobs = event.get("jobs") or JOBS_BY_SOURCE.get(source or "", [])
    if not jobs:
        return {"error": f"unknown source_id {source!r}; expected one of {sorted(JOBS_BY_SOURCE)} or a 'jobs' list"}
    target = get_settings().data_source
    if not (target.startswith("s3://") or target.startswith("dir:")):
        return {"error": "ALPHAFORGE_DATA_SOURCE must be an s3:// prefix or dir: path for ingest"}
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 900
    report = run(target, jobs, days=int(event.get("days") or 30), max_queries=int(event.get("max_queries") or 80),
                 max_seconds=max(60, int(remaining) - 120), use_gdelt=event.get("use_gdelt", True),
                 insider=event.get("insider", True))
    status = "ok" if all(r.get("status") == "ok" for r in report.values()) else \
        "error" if any(r.get("status") == "error" for r in report.values()) else "partial"
    store = get_store()
    for sid in ([source] if source else []):
        cfg = store.get("sources", sid) or {"source_id": sid}
        cfg.update(last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_status=status,
                   last_report={j: {k: v for k, v in r.items() if k not in ("errors",)} for j, r in report.items()})
        store.put("sources", sid, cfg)
    log.info("ingest source=%s jobs=%s status=%s", source, jobs, status)
    return {"status": status, "report": report}
