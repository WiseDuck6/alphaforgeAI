"""Load market data from a curated bundle and align it (calendar, CAD base, live extension).

The bundle can be a local folder or an S3 prefix, so everything goes through a
`read(name) -> dict | None` callable. Rules (same as data_pipeline/build_bundle.py):

- Calendar: union of TSX and U.S. trading days. A day is a market's trading day when at
  least half of that market's assets report a price on it.
- An equity missing on a union day (the other market's holiday) carries its last price for at
  most 3 days; crypto is sampled on the same days.
- CAD base: USD prices x Bank of Canada USD/CAD of the same day, else the last published rate.
- Live extension (live_extension.json, written by the ingest job) is appended after the
  starter data. Its adj_close was already spliced onto the starter series by the ingest job.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field

MAX_CARRY_DAYS = 3


@dataclass
class AssetSeries:
    ticker: str
    currency: str
    market: str                      # "TSX", "US" or "crypto"
    rows: list[dict]                 # own trading rows, listing currency, sorted by date
    local: list = field(default_factory=list)   # adj_close on the union calendar (None = no price)
    cad: list = field(default_factory=list)     # same in CAD
    carried: int = 0
    live_rows: int = 0


@dataclass
class MarketData:
    assets: dict[str, dict]
    series: dict[str, AssetSeries]
    dates: list[str]                 # union calendar
    fx: list[float]                  # USD/CAD on each union date
    rf_daily: list[float]            # Bank of Canada policy rate / 252 on each union date
    macro: dict[str, list[tuple[str, float]]]
    macro_meta: dict[str, dict]
    dividends: dict[str, list[tuple[str, float]]]
    starter_end: str
    live_end: str | None
    sources: dict


def market_of(asset: dict) -> str:
    if asset["asset_group"] == "crypto":
        return "crypto"
    return "TSX" if asset["ticker"].endswith(".TO") else "US"


def _value_on_or_before(points: list[tuple[str, float]], day: str):
    dates = [d for d, _ in points]
    i = bisect_right(dates, day) - 1
    return points[i][1] if i >= 0 else None


def _clean_points(points: list[dict]) -> list[dict]:
    out = []
    for p in points:
        adj = p.get("adj_close")
        if adj is None or not math.isfinite(adj) or adj <= 0:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p["date"])


def load_market_data(read) -> MarketData:
    assets_doc = read("assets.json")
    if not assets_doc:
        raise ValueError("bundle has no assets.json")
    assets = {a["ticker"]: a for a in assets_doc["assets"]}
    live = read("live_extension.json") or {}

    series: dict[str, AssetSeries] = {}
    starter_end = ""
    for t, a in assets.items():
        doc = read(f"prices/{t}.json") or {}
        rows = _clean_points(doc.get("points") or [])
        if rows:
            starter_end = max(starter_end, rows[-1]["date"])
        ext = _clean_points(((live.get("prices") or {}).get(t) or {}).get("points") or [])
        last = rows[-1]["date"] if rows else ""
        ext = [p for p in ext if p["date"] > last]
        series[t] = AssetSeries(t, a["listing_currency"], market_of(a), rows + ext, live_rows=len(ext))

    # Union calendar of TSX and U.S. trading days.
    by_market = defaultdict(list)
    for s in series.values():
        by_market[s.market].append(s)
    days: set[str] = set()
    for m in ("TSX", "US"):
        count = defaultdict(int)
        for s in by_market[m]:
            for r in s.rows:
                count[r["date"]] += 1
        need = math.ceil(len(by_market[m]) / 2)
        days |= {d for d, c in count.items() if c >= need}
    dates = sorted(days)
    if not dates:
        raise ValueError("no trading days in the bundle")

    # FX (USD/CAD), starter file plus live extension.
    fx_doc = read("fx_usdcad.json") or {}
    fx_points = sorted({d: float(v) for d, v in (fx_doc.get("points") or []) + (live.get("fx") or [])}.items())
    if not fx_points:
        raise ValueError("bundle has no fx_usdcad.json")
    fx_dates = [d for d, _ in fx_points]
    fx = []
    for d in dates:
        i = bisect_right(fx_dates, d) - 1
        fx.append(fx_points[max(i, 0)][1])

    # Macro series (long format), starter plus live extension.
    macro_doc = read("macro_series.json") or {}
    macro, macro_meta = {}, {}
    for sid, block in (macro_doc.get("series") or {}).items():
        pts = {d: float(v) for d, v in block.get("points") or []}
        pts.update({d: float(v) for d, v in ((live.get("macro") or {}).get(sid) or [])})
        macro[sid] = sorted(pts.items())
        macro_meta[sid] = {k: block.get(k) for k in ("label", "unit", "frequency", "source")}
    policy = macro.get("V39079") or []
    rf_daily = []
    for d in dates:
        v = _value_on_or_before(policy, d) if policy else None
        rf_daily.append((v or 0.0) / 100 / 252)

    # Dividends by ex-date.
    div_doc = read("dividends.json") or {}
    dividends = {}
    for t in assets:
        pts = {d: float(v) for d, v in ((div_doc.get("dividends") or {}).get(t) or [])}
        pts.update({d: float(v) for d, v in ((live.get("dividends") or {}).get(t) or [])})
        dividends[t] = sorted(pts.items())

    # Align every asset on the union calendar.
    for s in series.values():
        by_date = {r["date"]: r["adj_close"] for r in s.rows}
        first = s.rows[0]["date"] if s.rows else None
        last_v, run = None, 0
        for i, d in enumerate(dates):
            if first is None or d < first:
                s.local.append(None)
                continue
            if d in by_date:
                last_v, run = by_date[d], 0
                v = last_v
            elif last_v is not None and (s.market == "crypto" or run < MAX_CARRY_DAYS):
                # Crypto trades every day, so a missing union day only means the row is absent upstream.
                run += 1
                v = last_v
                s.carried += 1
            else:
                v = None
            s.local.append(v)
        s.cad = [None if v is None else v * (fx[i] if s.currency == "USD" else 1.0) for i, v in enumerate(s.local)]

    live_end = max((s.rows[-1]["date"] for s in series.values() if s.live_rows), default=None)
    return MarketData(
        assets=assets, series=series, dates=dates, fx=fx, rf_daily=rf_daily, macro=macro, macro_meta=macro_meta,
        dividends=dividends, starter_end=starter_end, live_end=live_end,
        sources={"live_extension": {k: live.get(k) for k in ("fetched_at", "through", "sources", "errors")} if live else None},
    )


def week_end_values(dates: list[str], values: list, start_index: int) -> list:
    """Value on the last union day of each ISO week from start_index on."""
    from datetime import date as _date

    out = {}
    for i in range(start_index, len(dates)):
        y, w, _ = _date.fromisoformat(dates[i]).isocalendar()
        out[(y, w)] = values[i]
    return [out[k] for k in sorted(out)]


def paired_returns(a: list, b: list) -> tuple[list[float], list[float]]:
    ra, rb = [], []
    for k in range(1, len(a)):
        if None in (a[k - 1], a[k], b[k - 1], b[k]) or not a[k - 1] or not b[k - 1]:
            continue
        ra.append(a[k] / a[k - 1] - 1)
        rb.append(b[k] / b[k - 1] - 1)
    return ra, rb
