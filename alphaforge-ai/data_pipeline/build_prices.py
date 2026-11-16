"""Write prices/<TICKER>.json (curated bundle) from the starter prices_daily.csv.

Contract: docs/data_contract.md, section "prices/<TICKER>.json".
Prices stay in each asset's listing currency (.TO in CAD, others in USD).
No forward-fill and no calendar alignment: one point per row Yahoo reported.

Usage (standard library only, Python 3.9+):
    python3 data_pipeline/build_prices.py
    python3 data_pipeline/build_prices.py --starter /path/to/alphaforge_starter --out /path/to/bundle/prices
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

TRANSFORM_VERSION = "build_prices 1.2"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STARTER = ROOT.parent / "alphaforge_starter"
DEFAULT_OUT = ROOT / "backend" / "alphaforge" / "data" / "fixtures" / "prices"
REQUIRED_COLUMNS = {"date", "ticker", "open", "high", "low", "close", "adj_close", "currency"}


def _positive_float(raw: str) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def build(starter: Path, out: Path) -> dict[str, int]:
    prices_csv = starter / "prices_daily.csv"
    universe_csv = starter / "assets_universe.csv"
    with universe_csv.open(newline="") as f:
        universe = {r["ticker"]: r["currency"] for r in csv.DictReader(f)}

    rows: dict[str, list[dict]] = defaultdict(list)
    dropped: dict[str, int] = defaultdict(int)
    ohlc_missing: dict[str, int] = defaultdict(int)
    with prices_csv.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{prices_csv} is missing columns {sorted(missing)}")
        for r in reader:
            ticker = r["ticker"]
            if ticker not in universe:
                raise SystemExit(f"{prices_csv}: ticker {ticker!r} is not in {universe_csv.name}")
            if r["currency"] != universe[ticker]:
                raise SystemExit(f"{prices_csv}: {ticker} on {r['date']} is in {r['currency']}, "
                                 f"universe says {universe[ticker]}")
            date.fromisoformat(r["date"])  # raises on a malformed date
            adj, close = _positive_float(r["adj_close"]), _positive_float(r["close"])
            if adj is None or close is None:
                dropped[ticker] += 1  # left out, not filled in
                continue
            try:
                volume = float(r.get("volume") or 0)
            except ValueError:
                volume = None
            point = {"date": r["date"], "adj_close": round(adj, 4), "close": round(close, 4),
                     "open": None, "high": None, "low": None,
                     "volume": None if volume is None or not math.isfinite(volume) else volume}
            o, h, lo = (_positive_float(r[k]) for k in ("open", "high", "low"))
            # Keep open/high/low only when the bar is consistent; otherwise leave them
            # null (candles skip the day) rather than guess.
            if None not in (o, h, lo) and h >= max(o, close, lo) and lo <= min(o, close):
                point.update(open=round(o, 4), high=round(h, 4), low=round(lo, 4))
            else:
                ohlc_missing[ticker] += 1
            rows[ticker].append(point)

    sha256 = hashlib.sha256(prices_csv.read_bytes()).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for ticker, points in sorted(rows.items()):
        points.sort(key=lambda p: p["date"])
        dates = [p["date"] for p in points]
        if len(set(dates)) != len(dates):
            raise SystemExit(f"{prices_csv}: duplicate dates for {ticker}")
        doc = {
            "schema_version": "1.0",
            "ticker": ticker,
            "currency": universe[ticker],
            "provenance": {
                "kind": "pipeline",
                "generated_at": date.today().isoformat(),
                "description": "Daily adj_close (Yahoo back-adjusted for splits and dividends) plus open, high, "
                               "low and close (split-adjusted only) and volume (shares; USD for crypto) in listing "
                               "currency. One point per reported row; no forward-fill, no calendar alignment, no FX "
                               "conversion.",
                "source_files": ["alphaforge_starter/prices_daily.csv"],
                "source_sha256": sha256,
                "transform_version": TRANSFORM_VERSION,
                "dropped_rows": dropped.get(ticker, 0),
                "ohlc_missing_rows": ohlc_missing.get(ticker, 0),
            },
            "points": points,
        }
        (out / f"{ticker}.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n")
        written[ticker] = len(points)

    absent = sorted(set(universe) - set(written))
    if absent:
        print(f"warning: no usable prices for {absent}", file=sys.stderr)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--starter", type=Path, default=DEFAULT_STARTER, help="folder with prices_daily.csv")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (the bundle's prices/)")
    args = ap.parse_args()
    written = build(args.starter, args.out)
    print(f"Wrote {len(written)} files to {args.out} ({sum(written.values())} points)")


if __name__ == "__main__":
    main()
