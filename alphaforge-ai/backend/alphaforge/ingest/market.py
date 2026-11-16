"""Live market data after the starter window: prices, dividends, USD/CAD and rates.

Sources (the same families as the starter dataset, for consistency):
- Yahoo Finance chart API (the starter prices came from Yahoo via yfinance).
- Bank of Canada Valet API: USD/CAD, policy rate, 2- and 10-year yields.
- FRED graph CSV: U.S. effective fed funds rate and 2- and 10-year Treasury yields.

Splice: Yahoo back-adjusts adj_close every time a dividend is paid, so a fresh download is
on a different scale from the starter file. The new rows are rescaled so that, on the last
date both series share (the anchor), the live adj_close equals the starter adj_close. Returns
after the anchor are therefore Yahoo's own, and nothing before the anchor changes. A split
after the anchor stops the splice for that ticker (reported as an error) instead of guessing.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from .http import Http, IngestHttpError

BOC_SERIES = ("V39079", "BD.CDN.2YR.DQ.YLD", "BD.CDN.10YR.DQ.YLD")
FRED_SERIES = ("DFF", "DGS2", "DGS10")


def _epoch(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


def yahoo_chart(http: Http, ticker: str, start: str, end: str) -> dict:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={_epoch(start)}&period2={_epoch(end) + 86400}"
           f"&interval=1d&events=div%2Csplit&includeAdjustedClose=true")
    doc = http.json(url, headers={"Accept": "application/json"})
    res = (doc.get("chart") or {}).get("result")
    if not res:
        raise IngestHttpError(url, 200, f"no chart result: {(doc.get('chart') or {}).get('error')}")
    r = res[0]
    offset = int((r.get("meta") or {}).get("gmtoffset") or 0)
    q = (r.get("indicators") or {}).get("quote", [{}])[0]
    adj = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, ts in enumerate(r.get("timestamp") or []):
        d = datetime.fromtimestamp(ts + offset, tz=timezone.utc).date().isoformat()
        vals = {k: (q.get(k) or [None] * (i + 1))[i] for k in ("open", "high", "low", "close", "volume")}
        a = adj[i] if i < len(adj) else None
        if a is None or vals["close"] is None:
            continue
        rows.append({"date": d, "adj_close": float(a), **{k: (None if v is None else float(v)) for k, v in vals.items()}})
    dedup = {p["date"]: p for p in rows}
    ev = r.get("events") or {}
    divs = sorted((datetime.fromtimestamp(int(k) + offset, tz=timezone.utc).date().isoformat(), float(v["amount"]))
                  for k, v in (ev.get("dividends") or {}).items())
    splits = sorted(datetime.fromtimestamp(int(k) + offset, tz=timezone.utc).date().isoformat()
                    for k in (ev.get("splits") or {}))
    return {"rows": [dedup[d] for d in sorted(dedup)], "dividends": divs, "splits": splits,
            "currency": (r.get("meta") or {}).get("currency"), "url": url.split("?")[0]}


def splice(base_points: list[dict], live: dict) -> tuple[list[dict], dict]:
    base = {p["date"]: p for p in base_points}
    starter_end = base_points[-1]["date"]
    common = [p for p in live["rows"] if p["date"] in base]
    if not common:
        raise ValueError("no overlapping date between the starter series and the live download")
    anchor = common[-1]
    factor = base[anchor["date"]]["adj_close"] / anchor["adj_close"]
    later_splits = [s for s in live["splits"] if s > anchor["date"]]
    if later_splits:
        raise ValueError(f"split after the anchor date ({', '.join(later_splits)}); splice skipped")
    out = []
    for p in live["rows"]:
        if p["date"] <= starter_end:
            continue
        out.append({"date": p["date"], "adj_close": round(p["adj_close"] * factor, 4),
                    **{k: (None if p[k] is None else round(p[k], 4)) for k in ("open", "high", "low", "close")},
                    "volume": p["volume"]})
    check = abs(base[anchor["date"]]["close"] / anchor["close"] - 1) if anchor.get("close") and base[anchor["date"]].get("close") else None
    return out, {"anchor_date": anchor["date"], "factor": round(factor, 8), "close_check": None if check is None else round(check, 6)}


def boc(http: Http, series: tuple[str, ...], start: str) -> dict[str, list[list]]:
    url = f"https://www.bankofcanada.ca/valet/observations/{','.join(series)}/json?start_date={start}"
    doc = http.json(url)
    out: dict[str, list[list]] = {s: [] for s in series}
    for obs in doc.get("observations", []):
        for s in series:
            v = (obs.get(s) or {}).get("v")
            if v not in (None, ""):
                out[s].append([obs["d"], float(v)])
    return out


def fred(http: Http, series_id: str, start: str) -> list[list]:
    text = http.text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        d = row.get("observation_date") or row.get("DATE")
        v = row.get(series_id)
        if d and v not in (None, "", "."):
            out.append([d, float(v)])
    return out


def run(http: Http, read, assets: dict[str, dict], today: str | None = None) -> dict:
    """Build live_extension.json from the starter end date (minus an overlap) to today."""
    today = today or date.today().isoformat()
    errors, prices, dividends, sources = [], {}, {}, {}
    starter_end = ""
    for t in assets:
        doc = read(f"prices/{t}.json") or {}
        pts = doc.get("points") or []
        if not pts:
            errors.append(f"{t}: no starter prices in the bundle")
            continue
        starter_end = max(starter_end, pts[-1]["date"])
        start = (date.fromisoformat(pts[-1]["date"]) - timedelta(days=10)).isoformat()
        try:
            live = yahoo_chart(http, t, start, today)
            points, info = splice(pts, live)
            prices[t] = {"currency": live["currency"], "points": points, "splice": info}
            dividends[t] = [[d, v] for d, v in live["dividends"] if d > pts[-1]["date"]]
        except (IngestHttpError, ValueError) as e:
            errors.append(f"{t}: {e}")
    sources["prices"] = "Yahoo Finance chart API (query1.finance.yahoo.com/v8/finance/chart)"
    fx_doc = read("fx_usdcad.json") or {}
    fx_start = (fx_doc.get("points") or [[starter_end or today]])[-1][0]
    fx, macro = [], {}
    try:
        fx = [p for p in boc(http, ("FXUSDCAD",), fx_start)["FXUSDCAD"] if p[0] > fx_start]
        sources["fx"] = "Bank of Canada Valet API, series FXUSDCAD"
    except IngestHttpError as e:
        errors.append(f"FX: {e}")
    macro_doc = read("macro_series.json") or {}
    last = {sid: (blk.get("points") or [[starter_end]])[-1][0] for sid, blk in (macro_doc.get("series") or {}).items()}
    try:
        got = boc(http, BOC_SERIES, min(last.get(s, starter_end) for s in BOC_SERIES))
        for s, pts in got.items():
            macro[s] = [p for p in pts if p[0] > last.get(s, starter_end)]
        sources["boc_rates"] = "Bank of Canada Valet API: " + ", ".join(BOC_SERIES)
    except IngestHttpError as e:
        errors.append(f"Bank of Canada rates: {e}")
    for s in FRED_SERIES:
        try:
            macro[s] = [p for p in fred(http, s, last.get(s, starter_end)) if p[0] > last.get(s, starter_end)]
        except IngestHttpError as e:
            errors.append(f"FRED {s}: {e}")
    sources["fred"] = "FRED graph CSV: " + ", ".join(FRED_SERIES)
    through = max((p["points"][-1]["date"] for p in prices.values() if p["points"]), default=None)
    return {
        "schema_version": "1.0",
        "provenance": {"kind": "live", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "description": "Rows after the starter window, spliced onto the starter adj_close (see splice)."},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "starter_end": starter_end, "through": through,
        "prices": prices, "dividends": dividends, "fx": fx, "macro": macro,
        "sources": sources, "errors": errors,
    }
