"""Run ingest jobs against a curated bundle (local folder or S3 prefix), then rebuild features.

    # from backend/, with network access
    python -m alphaforge.ingest.run --bundle ../data_pipeline/out --jobs market_data,sec_edgar,official_rss,gdelt
    python -m alphaforge.ingest.run --bundle ../data_pipeline/out --jobs news_backfill --max-queries 120

Jobs (each one maps to a monitoring source and an EventBridge Scheduler schedule on AWS):
    market_data    prices, dividends, USD/CAD and rates after the starter window -> live_extension.json
    sec_edgar      8-K / periodic filings -> signals.json; XBRL fundamentals and Form 4 -> fundamentals.json
    official_rss   Federal Reserve and Bank of Canada press releases, policy-rate decisions -> signals.json
    gdelt          recent articles per asset from allow-listed outlets -> signals.json
    news_backfill  articles around each asset's largest price moves since 2021 (resumable) -> signals.json
    features       rebuild asset_features.json only (always run after any other job)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..features import build_features
from . import edgar, market, news
from .enrich import comprehend_sentiment, is_promotional
from .http import Http
from .news import NewsSearch, article_signals
from .sources import NEWS_QUERIES

log = logging.getLogger("alphaforge.ingest")
HISTORY_START = "2021-11-01"
MAX_SIGNALS = 25_000
JOB_ORDER = ("market_data", "sec_edgar", "official_rss", "gdelt", "news_backfill", "features")


class DirIO:
    def __init__(self, path: Path):
        self.path = path
        self.label = f"dir:{path}"

    def read(self, name: str):
        p = self.path / name
        return json.loads(p.read_text()) if p.exists() else None

    def write(self, name: str, doc: dict) -> None:
        p = self.path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, separators=(",", ":"), default=str) + "\n")
        tmp.replace(p)


class S3IO:
    def __init__(self, uri: str):
        import boto3

        self.bucket, _, prefix = uri[len("s3://"):].partition("/")
        self.prefix = prefix.rstrip("/")
        self.s3 = boto3.client("s3")
        self.label = uri

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def read(self, name: str):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(name))
        except self.s3.exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    def write(self, name: str, doc: dict) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(name), ContentType="application/json",
                           Body=json.dumps(doc, separators=(",", ":"), default=str).encode())


def bundle_io(target: str):
    if target.startswith("s3://"):
        return S3IO(target)
    return DirIO(Path(target[4:] if target.startswith("dir:") else target).resolve())


def merge_signals(io, new: list[dict], note: str) -> dict:
    doc = io.read("signals.json") or {"schema_version": "1.0", "signals": []}
    # Stored headlines are re-checked against the current rules, so tightening the filter also
    # cleans what earlier runs kept.
    by_id = {s["signal_id"]: s for s in doc.get("signals", [])
             if not (s.get("kind") == "news" and is_promotional(s.get("title", "")))}
    added = 0
    for s in new:
        old = by_id.get(s["signal_id"])
        if old:
            old["tickers"] = sorted(set(old.get("tickers") or []) | set(s.get("tickers") or []))
            if s.get("linked_move") and not old.get("linked_move"):
                old["linked_move"] = s["linked_move"]
        else:
            by_id[s["signal_id"]] = s
            added += 1
    items = sorted(by_id.values(), key=lambda s: s.get("published_at", ""), reverse=True)[:MAX_SIGNALS]
    scored = comprehend_sentiment(items)
    counts: dict[str, int] = {}
    for s in items:
        counts[s.get("source_id", "?")] = counts.get(s.get("source_id", "?"), 0) + 1
    io.write("signals.json", {
        "schema_version": "1.0",
        "provenance": {"kind": "ingest", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "description": "Filings (SEC EDGAR), official statements (Federal Reserve, Bank of Canada), "
                                      "policy-rate decisions read from the macro series, and headlines from allow-listed "
                                      "outlets. Headline, publisher, date and link only; no article text is stored.",
                       "counts_by_source": counts, "last_update": note, "comprehend_scored": scored},
        "signals": items,
    })
    return {"added": added, "total": len(items)}


def _assets(io) -> dict[str, dict]:
    doc = io.read("assets.json")
    if not doc:
        raise SystemExit("bundle has no assets.json: build it with data_pipeline/build_bundle.py first")
    return {a["ticker"]: a for a in doc["assets"]}


def job_market_data(io, http, assets, ctx) -> dict:
    ext = market.run(http, io.read, assets, ctx["today"])
    io.write("live_extension.json", ext)
    return {"through": ext["through"], "tickers": len(ext["prices"]),
            "rows": sum(len(p["points"]) for p in ext["prices"].values()), "errors": ext["errors"]}


def job_sec_edgar(io, http, assets, ctx) -> dict:
    res = edgar.run(http, assets, HISTORY_START, ctx["today"], insider=ctx.get("insider", True))
    io.write("fundamentals.json", {
        "schema_version": "1.0",
        "provenance": {"kind": "ingest", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "description": "Annual XBRL facts (first-filed values, point in time) from SEC EDGAR company facts, "
                                      "shares outstanding from the latest cover page, and Form 4 insider trades (90 days).",
                       "source": "https://data.sec.gov/api/xbrl/companyfacts/"},
        "issuers": res["issuers"], "insider": res["insider"], "not_covered": res["not_covered"], "errors": res["errors"],
    })
    m = merge_signals(io, res["signals"], "sec_edgar")
    return {"issuers": len(res["issuers"]), "filing_signals": len(res["signals"]), **m, "errors": res["errors"][:20]}


def job_official_rss(io, http, assets, ctx) -> dict:
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    feed_items, errors = news.official_feeds(http, retrieved)
    events = news.macro_events(io.read("macro_series.json") or {}, retrieved, (io.read("live_extension.json") or {}).get("macro"))
    m = merge_signals(io, feed_items + events, "official_rss")
    return {"feed_items": len(feed_items), "policy_decisions": len(events), **m, "errors": errors}


def job_gdelt(io, http, assets, ctx) -> dict:
    """Recent articles per asset (the last `days` days, default 30)."""
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    end = ctx["today"]
    start = (date.fromisoformat(end) - timedelta(days=ctx.get("days", 30))).isoformat()
    ns = NewsSearch(http, use_gdelt=ctx.get("use_gdelt", True))
    new = []
    for t, a in assets.items():
        if t not in NEWS_QUERIES:
            continue
        arts = ns.search(t, start, end, max_items=12)
        new += article_signals(t, a["sector"], arts, retrieved)
    m = merge_signals(io, new, "gdelt")
    return {"window": f"{start} to {end}", "articles": len(new), **m, **ns.stats}


def job_news_backfill(io, http, assets, ctx) -> dict:
    """Articles published around each asset's largest price moves (2 days before to 1 day after)."""
    feats = io.read("asset_features.json")
    if not feats:
        return {"skipped": "asset_features.json not built yet"}
    state = io.read("news_backfill_state.json") or {"done": []}
    done = set(state["done"])
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ns = NewsSearch(http, use_gdelt=ctx.get("use_gdelt", True))
    budget = ctx.get("max_queries", 60)
    deadline = time.monotonic() + ctx.get("max_seconds", 780)
    new, queried = [], 0
    todo = []
    for t, a in assets.items():
        if t not in NEWS_QUERIES:
            continue
        for mv in (feats["assets"].get(t, {}).get("details", {}).get("big_moves") or []):
            key = f"{t}|{mv['date']}"
            if key not in done:
                todo.append((abs(mv["z"]), t, a, mv, key))
    todo.sort(key=lambda x: -x[0])  # largest moves first, so a partial run covers what matters most
    for _, t, a, mv, key in todo:
        if queried >= budget or time.monotonic() > deadline:
            break
        d = date.fromisoformat(mv["date"])
        arts = ns.search(t, (d - timedelta(days=2)).isoformat(), (d + timedelta(days=1)).isoformat(), max_items=6)
        queried += 1
        link = {"ticker": t, "date": mv["date"], "return": mv["return"], "z": mv["z"]}
        new += article_signals(t, a["sector"], arts, retrieved, linked_move=link)
        done.add(key)
    io.write("news_backfill_state.json", {"done": sorted(done), "updated_at": retrieved,
                                          "remaining": len(todo) - queried})
    m = merge_signals(io, new, "news_backfill")
    return {"queried_moves": queried, "remaining_moves": len(todo) - queried, "articles": len(new), **m, **ns.stats}


def job_features(io, http, assets, ctx) -> dict:
    doc = build_features(io.read)
    io.write("asset_features.json", doc)
    return {"as_of": doc["as_of"], "data_through": doc["data_through"], "signals_used": doc["signals_used"]}


JOBS = {"market_data": job_market_data, "sec_edgar": job_sec_edgar, "official_rss": job_official_rss,
        "gdelt": job_gdelt, "news_backfill": job_news_backfill, "features": job_features}


def run(target: str, jobs: list[str], **ctx) -> dict:
    io = bundle_io(target)
    http = Http()
    assets = _assets(io)
    ctx.setdefault("today", date.today().isoformat())
    wanted = [j for j in JOB_ORDER if j in jobs]
    if any(j != "features" for j in wanted) and "features" not in wanted:
        wanted.append("features")
    status = io.read("ingest_status.json") or {"jobs": {}}
    report = {}
    for j in wanted:
        started = time.monotonic()
        try:
            res = JOBS[j](io, http, assets, ctx)
            ok = "ok" if not res.get("errors") else "partial"
        except Exception as e:  # noqa: BLE001  (one failing source must not stop the others)
            log.exception("ingest job %s failed", j)
            res, ok = {"error": str(e)}, "error"
        res["seconds"] = round(time.monotonic() - started, 1)
        report[j] = {"status": ok, **res}
        status["jobs"][j] = {"last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": ok,
                             "summary": {k: v for k, v in res.items() if k != "errors"},
                             "errors": (res.get("errors") or [])[:10]}
        log.info("ingest %s %s %s", j, ok, {k: v for k, v in res.items() if k != "errors"})
    status["http_calls"] = http.calls
    io.write("ingest_status.json", status)
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Run AlphaForge ingest jobs against a curated bundle.")
    ap.add_argument("--bundle", required=True, help="bundle folder, dir:/path or s3://bucket/prefix")
    ap.add_argument("--jobs", default="market_data,sec_edgar,official_rss,gdelt", help=f"comma-separated: {','.join(JOB_ORDER)}")
    ap.add_argument("--days", type=int, default=30, help="look-back for the gdelt job")
    ap.add_argument("--max-queries", type=int, default=60, help="news_backfill: moves to search per run")
    ap.add_argument("--max-seconds", type=int, default=780, help="news_backfill: stop after this many seconds")
    ap.add_argument("--no-gdelt", action="store_true", help="search Google News RSS only")
    ap.add_argument("--no-insider", action="store_true", help="skip Form 4 parsing (faster)")
    args = ap.parse_args()
    report = run(args.bundle, args.jobs.split(","), days=args.days, max_queries=args.max_queries,
                 max_seconds=args.max_seconds, use_gdelt=not args.no_gdelt, insider=not args.no_insider)
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
