"""Build the curated bundle the backend reads (docs/data_contract.md) from the starter CSVs.

Writes to data_pipeline/out/ by default:
    assets.json               copied from the fixture after checking it against assets_universe.csv
    asset_metrics.json        per-window metrics in CAD, plus the 2022 stress scenario
    correlations.json         weekly-return correlation matrix per window
    macro_snapshot.json       latest macro values that were already published on the as-of date
    data_quality_report.json  calendar, FX, gaps, anomalies and sanity checks
    prices/<TICKER>.json      price history for charts (build_prices.py)

Use it with: ALPHAFORGE_DATA_SOURCE=dir:/abs/path/to/data_pipeline/out

Method (also recorded in the output):
- Returns come from adj_close only. Splits in corporate_actions.csv are never re-applied.
- Calendar: union of TSX and U.S. trading days. A day is a TSX (or U.S.) trading day when at least
  half of that market's assets report a row. An equity missing on a union day (the other market's
  holiday) carries its last price forward for at most 3 consecutive days, flagged; after that the
  day is a gap. Crypto is sampled on the same days, so weekend moves land in Monday's return.
- CAD base: USD prices x Bank of Canada USD/CAD of the same day, carried forward over Canadian
  holidays and flagged.
- Volatility, downside deviation and CVaR use daily simple returns. Correlations and beta use weekly
  returns (last trading day of each ISO week) to limit the effect of different closing times and holidays.

Standard library only, Python 3.9+:
    python3 data_pipeline/build_bundle.py [--starter DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import build_prices  # noqa: E402
from alphaforge.features import build_features, dir_reader  # noqa: E402  (standard library only)

TRANSFORM_VERSION = "build_bundle 1.1"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STARTER = ROOT.parent / "alphaforge_starter"
DEFAULT_OUT = ROOT / "data_pipeline" / "out"
ASSETS_SOURCE = ROOT / "backend" / "alphaforge" / "data" / "fixtures" / "assets.json"

# Window key -> first calendar day. Every window ends on the last trading day in the data.
WINDOWS = {"3y": "2023-11-13", "5y": "2021-11-12"}
STRESS_SCENARIOS = {
    "2022_rates_shock": {
        "label": "2022: stocks and bonds fell together",
        "start": "2022-01-03", "end": "2022-12-30", "base_date": "2021-12-31",
        "description": "Calendar-2022 total return in CAD (adj_close), from the last trading day of 2021 "
                       "to the last trading day of 2022. Independent of the metrics window.",
    },
}
MAX_CARRY_DAYS = 3
TTM_DAYS = 365
BIG_MOVE = 0.25  # one-day adjusted move worth flagging
KNOWN_ANOMALIES = [
    ("ZST.TO", "2022-07-06", "Zero-volume row (price unchanged). Kept as is."),
    ("CNQ.TO", "2022-08-22", "1.021 'split' on the same day as a 0.75 CAD dividend; likely a special distribution. "
                             "adj_close already reflects it; verify with CNQ investor relations before relying on it."),
]

# Macro series: publication lag rule (days after the reference date, or after the reference month's end).
MACRO_LAGS = {
    "V39079": ("daily", 0, "Bank of Canada policy rate; effective on the announcement date."),
    "BD.CDN.2YR.DQ.YLD": ("daily", 0, "Published by the Bank of Canada the same business day."),
    "BD.CDN.10YR.DQ.YLD": ("daily", 0, "Published by the Bank of Canada the same business day."),
    "DFF": ("daily", 1, "FRED posts the effective fed funds rate the next business day; 1 day assumed."),
    "DGS2": ("daily", 1, "FRED posts Treasury yields the next business day; 1 day assumed."),
    "DGS10": ("daily", 1, "FRED posts Treasury yields the next business day; 1 day assumed."),
    "CPIAUCSL": ("monthly", 15, "U.S. CPI: available about 15 days after the reference month (assumption)."),
    "STATIC_TOTALCPICHANGE": ("monthly", 21, "Canada CPI: available about 21 days after the reference month (assumption)."),
    "UNRATE": ("first_friday", None, "U.S. unemployment: available on the first Friday of the next month."),
}


def die(msg: str) -> None:
    raise SystemExit(f"build_bundle: {msg}")


def iso(d: date) -> str:
    return d.isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def r4(x):
    return None if x is None else round(x, 4)


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def market_of(row: dict) -> str:
    m = row["market"]
    if m == "TSX":
        return "TSX"
    if m.lower().startswith("crypto"):
        return "crypto"
    return "US"


# ----- loading ---------------------------------------------------------------
def load_inputs(starter: Path) -> dict:
    files = {name: starter / name for name in (
        "assets_universe.csv", "prices_daily.csv", "fx_usdcad_daily.csv", "corporate_actions.csv", "macro_rates.csv")}
    for name, p in files.items():
        if not p.exists():
            die(f"missing {p}")
    universe = read_csv(files["assets_universe.csv"])
    tickers = [u["ticker"] for u in universe]
    if len(set(tickers)) != len(tickers):
        die("duplicate tickers in assets_universe.csv")

    prices: dict[str, dict[str, dict]] = {t: {} for t in tickers}
    for r in read_csv(files["prices_daily.csv"]):
        t = r["ticker"]
        if t not in prices:
            die(f"prices_daily.csv: {t} is not in assets_universe.csv")
        try:
            date.fromisoformat(r["date"])
            adj, close, volume = float(r["adj_close"]), float(r["close"]), float(r["volume"] or 0)
        except ValueError:
            die(f"prices_daily.csv: unreadable row {r}")
        if not (math.isfinite(adj) and adj > 0 and math.isfinite(close) and close > 0):
            die(f"prices_daily.csv: non-positive price for {t} on {r['date']}")
        if r["date"] in prices[t]:
            die(f"prices_daily.csv: duplicate {t} {r['date']}")
        prices[t][r["date"]] = {"adj": adj, "close": close, "volume": volume, "currency": r["currency"]}
    for u in universe:
        if not prices[u["ticker"]]:
            die(f"no prices for {u['ticker']}")
        cur = {p["currency"] for p in prices[u["ticker"]].values()}
        if cur != {u["currency"]}:
            die(f"{u['ticker']}: price currency {cur} differs from universe currency {u['currency']}")

    fx = {}
    for r in read_csv(files["fx_usdcad_daily.csv"]):
        v = float(r["usdcad"])
        if not (math.isfinite(v) and v > 0):
            die(f"fx_usdcad_daily.csv: bad rate on {r['date']}")
        fx[r["date"]] = v

    actions = read_csv(files["corporate_actions.csv"])
    macro = read_csv(files["macro_rates.csv"])
    return {"files": files, "universe": universe, "prices": prices, "fx": fx, "actions": actions, "macro": macro}


def load_assets(universe: list[dict]) -> dict:
    doc = json.loads(ASSETS_SOURCE.read_text())
    by_ticker = {a["ticker"]: a for a in doc["assets"]}
    if set(by_ticker) != {u["ticker"] for u in universe}:
        die(f"{ASSETS_SOURCE.name} tickers differ from assets_universe.csv")
    for u in universe:
        a = by_ticker[u["ticker"]]
        for field_csv, field_json in (("name", "name"), ("asset_class", "asset_class"), ("currency", "listing_currency"),
                                      ("sector", "sector")):
            if a[field_json] != u[field_csv]:
                die(f"{u['ticker']}: {field_json} '{a[field_json]}' differs from assets_universe.csv '{u[field_csv]}'")
    return doc


# ----- calendar and aligned prices -------------------------------------------
def build_calendar(universe: list[dict], prices: dict) -> tuple[list[str], dict]:
    by_market = defaultdict(list)
    for u in universe:
        by_market[market_of(u)].append(u["ticker"])
    days = {}
    off_calendar = []
    for m in ("TSX", "US"):
        count = defaultdict(int)
        for t in by_market[m]:
            for d in prices[t]:
                count[d] += 1
        need = math.ceil(len(by_market[m]) / 2)
        days[m] = {d for d, c in count.items() if c >= need}
        for t in by_market[m]:
            off_calendar += [(t, d) for d in prices[t] if d not in days[m]]
    union = sorted(days["TSX"] | days["US"])
    info = {
        "rule": "Union of TSX and U.S. trading days; a market's trading day is a date on which at least half of its "
                "assets report a price.",
        "tsx_days": len(days["TSX"]), "us_days": len(days["US"]), "union_days": len(union),
        "tsx_only_days": len(days["TSX"] - days["US"]), "us_only_days": len(days["US"] - days["TSX"]),
        "start": union[0], "end": union[-1],
        "off_calendar_rows": [{"ticker": t, "date": d} for t, d in sorted(off_calendar)],
        "max_carry_days": MAX_CARRY_DAYS,
    }
    return union, info


def fx_on(union: list[str], fx: dict) -> tuple[list[float], list[bool]]:
    fx_dates = sorted(fx)
    rates, carried = [], []
    for d in union:
        i = bisect_right(fx_dates, d) - 1
        if i < 0:
            die(f"no USD/CAD rate on or before {d}")
        rates.append(fx[fx_dates[i]])
        carried.append(fx_dates[i] != d)
    return rates, carried


def align(ticker: str, rows: dict, union: list[str], usd: bool, rates: list[float]) -> dict:
    """Prices on the union calendar: local adj_close, CAD adj_close, and flags."""
    first = min(rows)
    local, cad, carried = [], [], []
    run, last = 0, None
    for i, d in enumerate(union):
        if d < first:
            local.append(None); cad.append(None); carried.append(False)
            continue
        if d in rows:
            last, run = rows[d]["adj"], 0
            value, flag = last, False
        elif last is not None and run < MAX_CARRY_DAYS:
            run += 1
            value, flag = last, True
        else:
            value, flag = None, False
        local.append(value)
        carried.append(flag)
        cad.append(None if value is None else value * (rates[i] if usd else 1.0))
    return {"ticker": ticker, "local": local, "cad": cad, "carried": carried, "first": first}


# ----- metrics -----------------------------------------------------------------
def window_indices(union: list[str], start: str) -> tuple[int, int]:
    i0 = next(i for i, d in enumerate(union) if d >= start)
    return i0, len(union) - 1


def simple_returns(values: list) -> list[float]:
    out, prev = [], None
    for v in values:
        if v is None:
            continue
        if prev is not None:
            out.append(v / prev - 1)
        prev = v
    return out


def week_end_values(union: list[str], values: list, i0: int, i1: int) -> list:
    """Value on the last union day of each ISO week inside [i0, i1] (None if missing that day)."""
    out = {}
    for i in range(i0, i1 + 1):
        y, w, _ = date.fromisoformat(union[i]).isocalendar()
        out[(y, w)] = values[i]
    return [out[k] for k in sorted(out)]


def paired_returns(a: list, b: list) -> tuple[list[float], list[float]]:
    ra, rb = [], []
    for k in range(1, len(a)):
        if None in (a[k - 1], a[k], b[k - 1], b[k]):
            continue
        ra.append(a[k] / a[k - 1] - 1)
        rb.append(b[k] / b[k - 1] - 1)
    return ra, rb


def pearson(x: list[float], y: list[float]):
    if len(x) < 3:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def beta(x: list[float], m: list[float]):
    if len(x) < 3:
        return None
    mx, mm = statistics.fmean(x), statistics.fmean(m)
    var = sum((b - mm) ** 2 for b in m)
    return None if var == 0 else sum((a - mx) * (b - mm) for a, b in zip(x, m)) / var


def drawdown_stats(values: list) -> tuple[float, int | None]:
    """Max drawdown (<= 0) and trading days from its trough back to the prior peak (None if not recovered)."""
    pts = [v for v in values if v is not None]
    peak, mdd, trough_i, peak_at_trough = pts[0], 0.0, None, None
    for i, v in enumerate(pts):
        peak = max(peak, v)
        dd = v / peak - 1
        if dd < mdd:
            mdd, trough_i, peak_at_trough = dd, i, peak
    if trough_i is None:
        return 0.0, 0
    for j in range(trough_i + 1, len(pts)):
        if pts[j] >= peak_at_trough:
            return mdd, j - trough_i
    return mdd, None


def worst_month(union: list[str], values: list, i0: int, i1: int):
    """Worst return between consecutive month-end values, complete calendar months only."""
    end_day = date.fromisoformat(union[i1])
    month_end = {}
    for i in range(i0, i1 + 1):
        if values[i] is not None:
            month_end[union[i][:7]] = values[i]
    def complete(ym: str) -> bool:
        y, m = map(int, ym.split("-"))
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        return last <= end_day
    keys = [k for k in sorted(month_end) if complete(k)]
    rets = [month_end[keys[k]] / month_end[keys[k - 1]] - 1 for k in range(1, len(keys))]
    return min(rets) if rets else None


def value_on_or_before(union: list[str], values: list, day: str):
    i = bisect_right(union, day) - 1
    while i >= 0 and values[i] is None:
        i -= 1
    return (values[i], union[i]) if i >= 0 else (None, None)


def ttm_dividend_yield(ticker: str, rows: dict, actions: list[dict], end: str, warnings: list) -> float:
    end_d = date.fromisoformat(end)
    start = iso(end_d - timedelta(days=TTM_DAYS))
    mine = [a for a in actions if a["ticker"] == ticker]
    splits = [a for a in mine if a["action"] == "split" and start < a["ex_date"] <= end]
    if splits:
        warnings.append(f"{ticker}: split inside the trailing-12-month dividend window {start} to {end}; check the yield.")
    total = sum(float(a["value"]) for a in mine if a["action"] == "dividend" and start < a["ex_date"] <= end)
    last_day = max(d for d in rows if d <= end)
    return total / rows[last_day]["close"]


def median_dollar_volume(ticker: str, rows: dict, crypto: bool, usd: bool, fx: dict, start: str, end: str):
    fx_dates = sorted(fx)
    vals = []
    for d, r in rows.items():
        if not (start <= d <= end):
            continue
        # Yahoo reports crypto volume in quote currency (USD); equities and ETFs in shares.
        amount = r["volume"] if crypto else r["volume"] * r["close"]
        if usd:
            amount *= fx[fx_dates[bisect_right(fx_dates, d) - 1]]
        vals.append(amount)
    return statistics.median(vals) if vals else None


def compute_metrics(inp: dict, union: list[str], aligned: dict, warnings: list) -> tuple[dict, dict]:
    spy = aligned["SPY"]["cad"]
    windows, corr_windows = {}, {}
    universe = inp["universe"]
    tickers = [u["ticker"] for u in universe]
    for key, start in WINDOWS.items():
        i0, i1 = window_indices(union, start)
        week_ends = {t: week_end_values(union, aligned[t]["cad"], i0, i1) for t in tickers}
        metrics = {}
        for u in universe:
            t = u["ticker"]
            vals = aligned[t]["cad"][i0:i1 + 1]
            present = [v for v in vals if v is not None]
            if len(present) < 2:
                metrics[t] = None
                warnings.append(f"{t}: fewer than 2 prices in window {key}; no metrics.")
                continue
            first_i = next(i for i, v in enumerate(vals) if v is not None)
            last_i = max(i for i, v in enumerate(vals) if v is not None)
            years = (date.fromisoformat(union[i0 + last_i]) - date.fromisoformat(union[i0 + first_i])).days / 365.25
            rets = simple_returns(vals)
            ppy = len(rets) / years
            mdd, recovery = drawdown_stats(vals)
            downs = sorted(rets)
            tail = downs[:max(1, int(len(downs) * 0.05))]
            wa, wm = paired_returns(week_ends[t], week_ends["SPY"])
            mdv = median_dollar_volume(t, inp["prices"][t], market_of(u) == "crypto", u["currency"] == "USD",
                                       inp["fx"], union[i0], union[i1])
            stress = {}
            for sid, sc in STRESS_SCENARIOS.items():
                p0, d0 = value_on_or_before(union, aligned[t]["cad"], sc["base_date"])
                p1, d1 = value_on_or_before(union, aligned[t]["cad"], sc["end"])
                stress[sid] = r4(p1 / p0 - 1) if p0 and p1 and d0 < d1 and d0 >= aligned[t]["first"] else None
            metrics[t] = {
                "ann_return": r4((present[-1] / present[0]) ** (1 / years) - 1),
                "ann_vol": r4(statistics.stdev(rets) * math.sqrt(ppy)),
                "max_drawdown": r4(mdd),
                "downside_dev": r4(math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets)) * math.sqrt(ppy)),
                "cvar_95": r4(statistics.fmean(tail)),
                "worst_month": r4(worst_month(union, aligned[t]["cad"], i0, i1)),
                "recovery_days": recovery,
                "beta_spy": r4(beta(wa, wm)),
                "corr_spy": r4(pearson(wa, wm)),
                "dividend_yield_ttm": r4(ttm_dividend_yield(t, inp["prices"][t], inp["actions"], union[i1], warnings)),
                "median_dollar_volume": None if mdv is None else round(mdv),
                "history_days": len(rets),
                "data_gaps": sum(1 for v in vals[first_i:] if v is None),
                "carried_days": sum(aligned[t]["carried"][i0:i1 + 1]),
                "stress": stress,
            }
        matrix = []
        for a in tickers:
            row = []
            for b in tickers:
                if a == b:
                    row.append(1.0)
                    continue
                c = pearson(*paired_returns(week_ends[a], week_ends[b]))
                if c is None:
                    die(f"cannot compute correlation {a}/{b} in window {key}")
                row.append(r4(c))
            matrix.append(row)
        windows[key] = {"start": union[i0], "end": union[i1], "trading_days": i1 - i0 + 1, "metrics": metrics}
        corr_windows[key] = {"start": union[i0], "end": union[i1], "frequency": "weekly",
                             "observations": len(week_ends["SPY"]) - 1, "tickers": tickers, "matrix": matrix}
    return windows, corr_windows


# ----- macro snapshot -----------------------------------------------------------
def first_friday_after_month(ym_first_day: date) -> date:
    nxt = date(ym_first_day.year + (ym_first_day.month == 12), ym_first_day.month % 12 + 1, 1)
    return nxt + timedelta(days=(4 - nxt.weekday()) % 7)


def available_date(series_id: str, ref: date) -> date:
    rule, lag, _ = MACRO_LAGS[series_id]
    if rule == "daily":
        return ref + timedelta(days=lag)
    if rule == "monthly":
        month_end = date(ref.year + (ref.month == 12), ref.month % 12 + 1, 1) - timedelta(days=1)
        return month_end + timedelta(days=lag)
    return first_friday_after_month(ref)


def macro_snapshot(macro: list[dict], as_of: str, warnings: list) -> dict:
    as_of_d = date.fromisoformat(as_of)
    by = defaultdict(list)
    meta = {}
    for r in macro:
        sid = r["series_id"]
        if sid not in MACRO_LAGS:
            warnings.append(f"macro_rates.csv: series {sid} has no publication-lag rule; left out of the snapshot.")
            continue
        try:
            value = float(r["value"])
        except ValueError:
            continue
        by[sid].append((date.fromisoformat(r["date"]), value))
        meta[sid] = {k: r[k] for k in ("label", "unit", "frequency", "source")}
    series = []
    for sid in MACRO_LAGS:
        if sid not in by:
            continue
        obs = sorted((d, v, available_date(sid, d)) for d, v in by[sid])
        usable = [o for o in obs if o[2] <= as_of_d]
        if not usable:
            continue
        d, v, avail = usable[-1]
        item = {"series_id": sid, **meta[sid], "value": v, "reference_date": iso(d), "available_date": iso(avail),
                "availability_rule": MACRO_LAGS[sid][2]}
        if sid == "CPIAUCSL":
            prior = [o for o in usable if o[0] == date(d.year - 1, d.month, 1)]
            item["yoy_change"] = r4(v / prior[0][1] - 1) if prior else None
        series.append(item)
    return {"as_of": as_of, "series": series,
            "note": "Each value is the latest one already published on the as-of date (no look-ahead). "
                    "FRED values are the latest revision, not as first published."}


# ----- quality report -------------------------------------------------------------
def quality_report(inp: dict, union: list[str], cal: dict, fx_carried: list[bool], aligned: dict,
                   windows: dict, warnings: list) -> dict:
    per_asset = {}
    prices = inp["prices"]
    for u in inp["universe"]:
        t = u["ticker"]
        rows = prices[t]
        ds = sorted(rows)
        zero_vol = [d for d in ds if rows[d]["volume"] == 0 and market_of(u) != "crypto"]
        big = []
        for a, b in zip(ds, ds[1:]):
            move = rows[b]["adj"] / rows[a]["adj"] - 1
            if abs(move) > BIG_MOVE:
                big.append({"date": b, "move": r4(move)})
        per_asset[t] = {
            "rows": len(rows), "first": ds[0], "last": ds[-1],
            "carried_days": sum(aligned[t]["carried"]), "gaps": sum(1 for v in aligned[t]["cad"] if v is None) -
            sum(1 for d in union if d < aligned[t]["first"]),
            "zero_volume_rows": zero_vol, "big_one_day_moves": big,
        }
        if zero_vol and not any(k[0] == t for k in KNOWN_ANOMALIES):
            warnings.append(f"{t}: zero-volume rows on {', '.join(zero_vol)}.")
        if big:
            warnings.append(f"{t}: one-day adjusted move above {BIG_MOVE:.0%} on {', '.join(b['date'] for b in big)}.")
    for t, d, _ in KNOWN_ANOMALIES:
        if t in prices and d not in prices[t]:
            warnings.append(f"{t}: expected anomaly date {d} is not in the data.")
    if cal["off_calendar_rows"]:
        warnings.append(f"{len(cal['off_calendar_rows'])} equity rows fall on days their market was closed; "
                        "they are used only if the other market was open.")

    sanity = {}
    for t in ("SPY", "TLT", "XBB.TO", "GLD"):
        sc = STRESS_SCENARIOS["2022_rates_shock"]
        out = {}
        for k in ("cad", "local"):
            p0, _ = value_on_or_before(union, aligned[t][k], sc["base_date"])
            p1, _ = value_on_or_before(union, aligned[t][k], sc["end"])
            out[f"calendar_2022_return_{k}"] = r4(p1 / p0 - 1)
        sanity[t] = out
    return {
        "calendar": cal,
        "fx": {"series": "Bank of Canada FXUSDCAD", "union_days_with_carried_rate": sum(fx_carried),
               "rule": "Rate of the same day, else the last published rate (Canadian holidays)."},
        "windows": {k: {"start": w["start"], "end": w["end"], "trading_days": w["trading_days"]} for k, w in windows.items()},
        "known_anomalies": [{"ticker": t, "date": d, "note": n} for t, d, n in KNOWN_ANOMALIES],
        "per_asset": per_asset,
        "sanity_checks": {
            "description": "Calendar-2022 returns, CAD and listing currency. Expected roughly: CAD SPY -13%, TLT -24%, "
                           "XBB.TO -11%, GLD +7%; USD SPY -18%, TLT -31%.",
            "values": sanity,
        },
        "limitations": [
            "Survivorship bias: the universe was chosen in 2026 among names that are large today.",
            "adj_close is Yahoo's back-adjusted series; older values change when the data is re-downloaded.",
            "Crypto is sampled on TSX/U.S. trading days; its UTC daily close differs from the 16:00 ET equity close.",
            "Dividend yields use corporate_actions.csv, which may be incomplete for ETFs. Assets without any dividend rows get 0.",
            "Monthly macro values use assumed publication lags; FRED values are revised, not first-release.",
        ],
        "warnings": warnings,
    }


# ----- main ---------------------------------------------------------------------
def build(starter: Path, out: Path) -> dict:
    inp = load_inputs(starter)
    assets_doc = load_assets(inp["universe"])
    union, cal = build_calendar(inp["universe"], inp["prices"])
    rates, fx_carried = fx_on(union, inp["fx"])
    aligned = {u["ticker"]: align(u["ticker"], inp["prices"][u["ticker"]], union, u["currency"] == "USD", rates)
               for u in inp["universe"]}
    warnings: list[str] = []
    windows, corr = compute_metrics(inp, union, aligned, warnings)
    today = date.today().isoformat()
    shas = {f"alphaforge_starter/{p.name}": sha256(p) for p in inp["files"].values()}

    def prov(description: str, sources: list[str]) -> dict:
        return {"kind": "pipeline", "generated_at": today, "description": description,
                "source_files": [f"alphaforge_starter/{s}" for s in sources],
                "source_sha256": {f"alphaforge_starter/{s}": shas[f"alphaforge_starter/{s}"] for s in sources},
                "transform_version": TRANSFORM_VERSION}

    metrics_doc = {
        "schema_version": "1.0",
        "provenance": prov(
            "Per-window metrics in CAD from adj_close on the union of TSX and U.S. trading days (equities carried forward "
            "at most 3 days, crypto sampled on the same days). ann_return: annualised compound return. ann_vol, "
            "downside_dev: daily simple returns, annualised with the observed trading days per year. cvar_95: mean of "
            "the worst 5% daily returns. worst_month: complete calendar months. recovery_days: trading days from the "
            "max-drawdown trough back to the prior peak (null if not recovered). beta_spy, corr_spy: weekly returns vs "
            "SPY in CAD. dividend_yield_ttm: dividends with ex-date in the last 365 days / last close (listing currency). "
            "median_dollar_volume: CAD.",
            ["prices_daily.csv", "fx_usdcad_daily.csv", "corporate_actions.csv"]),
        "base_currency": "CAD",
        "stress_scenarios": {k: {f: v[f] for f in ("label", "start", "end", "description")}
                             for k, v in STRESS_SCENARIOS.items()},
        "windows": windows,
    }
    corr_doc = {"schema_version": "1.0",
                "provenance": prov("Pearson correlation of weekly simple returns in CAD (last trading day of each ISO week).",
                                   ["prices_daily.csv", "fx_usdcad_daily.csv"]),
                "windows": corr}
    macro_doc = {"schema_version": "1.0",
                 "provenance": prov("Latest macro values available on the as-of date, with publication-lag rules.",
                                    ["macro_rates.csv"]),
                 **macro_snapshot(inp["macro"], union[-1], warnings)}
    report = {"schema_version": "1.0",
              "provenance": prov("Validation of the starter data and of the pipeline's alignment choices.",
                                 ["assets_universe.csv", "prices_daily.csv", "fx_usdcad_daily.csv", "corporate_actions.csv",
                                  "macro_rates.csv"]),
              **quality_report(inp, union, cal, fx_carried, aligned, windows, warnings)}

    fx_doc = {"schema_version": "1.0",
              "provenance": prov("Bank of Canada USD/CAD as published (no rows on Canadian holidays).", ["fx_usdcad_daily.csv"]),
              "points": [[d, v] for d, v in sorted(inp["fx"].items())]}
    series: dict[str, dict] = {}
    for r in inp["macro"]:
        try:
            value = float(r["value"])
        except ValueError:
            continue
        block = series.setdefault(r["series_id"], {k: r[k] for k in ("label", "unit", "frequency", "source")} | {"points": []})
        block["points"].append([r["date"], value])
    for block in series.values():
        block["points"].sort()
    macro_series_doc = {"schema_version": "1.0",
                        "provenance": prov("Macro series in long format, dated by reference date (see macro_snapshot.json "
                                           "for publication lags).", ["macro_rates.csv"]),
                        "series": series}
    divs: dict[str, list] = defaultdict(list)
    splits: dict[str, list] = defaultdict(list)
    for a in inp["actions"]:
        (divs if a["action"] == "dividend" else splits)[a["ticker"]].append([a["ex_date"], float(a["value"])])
    dividends_doc = {"schema_version": "1.0",
                     "provenance": prov("Dividends per share (listing currency) and split ratios by ex-date.",
                                        ["corporate_actions.csv"]),
                     "dividends": {t: sorted(v) for t, v in divs.items()},
                     "splits": {t: sorted(v) for t, v in splits.items()}}

    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS_SOURCE, out / "assets.json")
    for name, doc in (("asset_metrics.json", metrics_doc), ("correlations.json", corr_doc),
                      ("macro_snapshot.json", macro_doc), ("data_quality_report.json", report)):
        (out / name).write_text(json.dumps(doc, indent=1) + "\n")
    for name, doc in (("fx_usdcad.json", fx_doc), ("macro_series.json", macro_series_doc), ("dividends.json", dividends_doc)):
        (out / name).write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    build_prices.build(starter, out / "prices")
    # Features use the files just written plus any ingest outputs already in the folder
    # (live_extension.json, fundamentals.json, signals.json), which this script never deletes.
    features = build_features(dir_reader(out))
    (out / "asset_features.json").write_text(json.dumps(features, separators=(",", ":")) + "\n")
    return {"out": str(out), "windows": {k: (w["start"], w["end"]) for k, w in windows.items()},
            "warnings": warnings, "assets": len(assets_doc["assets"])}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the curated bundle from the starter CSVs.")
    ap.add_argument("--starter", type=Path, default=DEFAULT_STARTER, help="folder with the starter CSVs")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (the bundle)")
    args = ap.parse_args()
    res = build(args.starter, args.out)
    print(f"Wrote bundle for {res['assets']} assets to {res['out']}")
    for k, (s, e) in res["windows"].items():
        print(f"  window {k}: {s} to {e}")
    for w in res["warnings"]:
        print(f"  warning: {w}")


if __name__ == "__main__":
    main()
