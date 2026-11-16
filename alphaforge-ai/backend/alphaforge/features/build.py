"""Build asset_features.json: the market, fundamentals and news features behind the fit score.

    python -m alphaforge.features.build --bundle /abs/path/to/data_pipeline/out

Reads the curated bundle (prices, FX, macro, dividends, optional live extension,
fundamentals and signals) and writes asset_features.json next to it. The same function runs
in the ingest Lambda after each refresh, against the S3 copy of the bundle.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import indicators as ind
from .fundamentals import ratios
from .market import MarketData, load_market_data, paired_returns, week_end_values
from .news import by_ticker, earnings_reactions, insider_features, news_features

SCHEMA_VERSION = "1.0"
TRANSFORM_VERSION = "features 1.0"
N = {"1m": 21, "3m": 63, "6m": 126, "1y": 252, "3y": 756}
STRESS_2022 = ("2021-12-31", "2022-12-30")
FUNDAMENTAL_KEYS = ("market_cap", "revenue_growth", "earnings_growth", "roe", "roa", "roic", "pe", "pb", "ev_ebitda",
                    "debt_to_equity", "net_debt_ebitda", "fcf", "fcf_yield", "payout_ratio")
FACTOR_PROXIES = {"market": "SPY", "growth_tilt": ("QQQ", "SPY"), "rates": "TLT", "gold": "GLD"}

# key: (label, group, unit, window, method)
DEFINITIONS: dict[str, tuple[str, str, str, str, str]] = {
    "ret_1m": ("Return, 1 month", "returns", "fraction", "21 trading days", "Total return in CAD from adj_close."),
    "ret_3m": ("Return, 3 months", "returns", "fraction", "63 trading days", "Total return in CAD from adj_close."),
    "ret_6m": ("Return, 6 months", "returns", "fraction", "126 trading days", "Total return in CAD from adj_close."),
    "ret_1y": ("Return, 1 year", "returns", "fraction", "252 trading days", "Total return in CAD from adj_close."),
    "ret_3y": ("Return, 3 years", "returns", "fraction", "756 trading days", "Total return in CAD from adj_close."),
    "cagr_3y": ("Compound annual growth, 3 years", "returns", "fraction", "756 trading days", "(end/start)^(1/years) - 1, CAD."),
    "cagr_5y": ("Compound annual growth, 5 years", "returns", "fraction", "full history", "(end/start)^(1/years) - 1, CAD."),
    "vol_1m": ("Volatility, 1 month", "risk", "fraction", "21 trading days", "Stdev of daily CAD returns x sqrt(252)."),
    "vol_3m": ("Volatility, 3 months", "risk", "fraction", "63 trading days", "Stdev of daily CAD returns x sqrt(252)."),
    "vol_6m": ("Volatility, 6 months", "risk", "fraction", "126 trading days", "Stdev of daily CAD returns x sqrt(252)."),
    "vol_1y": ("Volatility, 1 year", "risk", "fraction", "252 trading days", "Stdev of daily CAD returns x sqrt(252)."),
    "downside_vol_1y": ("Downside volatility, 1 year", "risk", "fraction", "252 trading days",
                        "Root mean square of negative daily returns x sqrt(252)."),
    "mdd_1y": ("Maximum drawdown, 1 year", "risk", "fraction", "252 trading days", "Largest peak-to-trough fall, CAD."),
    "mdd_3y": ("Maximum drawdown, 3 years", "risk", "fraction", "756 trading days", "Largest peak-to-trough fall, CAD."),
    "mdd_5y": ("Maximum drawdown, 5 years", "risk", "fraction", "full history", "Largest peak-to-trough fall, CAD."),
    "recovery_days_5y": ("Recovery time after the worst fall", "risk", "days", "full history",
                         "Trading days from the 5-year drawdown trough back to the prior peak; empty if not recovered."),
    "stress_2022": ("Calendar-2022 return", "risk", "fraction", "2021-12-31 to 2022-12-30",
                    "Return in CAD over 2022, when stocks and bonds fell together."),
    "var_95_1d": ("Value at risk, 95%, one day", "tail", "fraction", "756 trading days",
                  "5th percentile of daily CAD returns, as a loss."),
    "es_95_1d": ("Expected shortfall (CVaR), 95%, one day", "tail", "fraction", "756 trading days",
                 "Average of the worst 5% daily CAD returns, as a loss. CVaR and expected shortfall are the same measure."),
    "beta_1y": ("Beta vs S&P 500 (SPY), 1 year", "risk_adjusted", "ratio", "52 weeks", "Weekly CAD returns regressed on SPY."),
    "beta_3y": ("Beta vs S&P 500 (SPY), 3 years", "risk_adjusted", "ratio", "156 weeks", "Weekly CAD returns regressed on SPY."),
    "sharpe_1y": ("Sharpe ratio, 1 year", "risk_adjusted", "ratio", "252 trading days",
                  "Annualised mean daily return above the Bank of Canada policy rate / annualised volatility."),
    "sharpe_3y": ("Sharpe ratio, 3 years", "risk_adjusted", "ratio", "756 trading days",
                  "Annualised mean daily return above the Bank of Canada policy rate / annualised volatility."),
    "sortino_1y": ("Sortino ratio, 1 year", "risk_adjusted", "ratio", "252 trading days",
                   "Annualised mean excess return / annualised downside deviation below the policy rate."),
    "sortino_3y": ("Sortino ratio, 3 years", "risk_adjusted", "ratio", "756 trading days",
                   "Annualised mean excess return / annualised downside deviation below the policy rate."),
    "rel_sector_1y": ("1-year return vs sector peers", "relative", "fraction", "252 trading days",
                      "Asset return minus the average return of the other universe stocks in the same sector."),
    "rel_market_1y": ("1-year return vs home market", "relative", "fraction", "252 trading days",
                      "Asset return minus XIU.TO (Canadian listings) or SPY (others)."),
    "rsi_14": ("RSI, 14 days", "technical", "index", "14 trading rows", "Wilder's relative strength index on adj_close (listing currency)."),
    "sma_20": ("Simple moving average, 20 days", "technical", "price", "20 trading rows", "Average adj_close, listing currency."),
    "sma_50": ("Simple moving average, 50 days", "technical", "price", "50 trading rows", "Average adj_close, listing currency."),
    "sma_200": ("Simple moving average, 200 days", "technical", "price", "200 trading rows", "Average adj_close, listing currency."),
    "px_vs_sma_20": ("Price vs 20-day average", "technical", "fraction", "20 trading rows", "Last adj_close / SMA20 - 1."),
    "px_vs_sma_50": ("Price vs 50-day average", "technical", "fraction", "50 trading rows", "Last adj_close / SMA50 - 1."),
    "px_vs_sma_200": ("Price vs 200-day average", "technical", "fraction", "200 trading rows", "Last adj_close / SMA200 - 1."),
    "macd": ("MACD line", "technical", "price", "EMA 12 and 26", "EMA12 - EMA26 of adj_close."),
    "macd_signal": ("MACD signal line", "technical", "price", "EMA 9 of MACD", "9-day EMA of the MACD line."),
    "macd_hist_pct": ("MACD histogram / price", "technical", "fraction", "12, 26, 9", "(MACD - signal) / last price."),
    "atr_14": ("Average true range, 14 days", "technical", "price", "14 trading rows", "Wilder's ATR on high, low, close."),
    "atr_pct": ("ATR as share of price", "technical", "fraction", "14 trading rows", "ATR14 / last close."),
    "pct_from_52w_high": ("Distance from 52-week high", "technical", "fraction", "252 trading rows", "Last adj_close / highest adj_close - 1."),
    "adv_30": ("Average dollar volume, 30 days", "liquidity", "cad", "30 trading rows", "Mean of volume x close, in CAD."),
    "adv_90": ("Average dollar volume, 90 days", "liquidity", "cad", "90 trading rows", "Mean of volume x close, in CAD."),
    "corr_spy_1y": ("Correlation with SPY, 1 year", "risk_adjusted", "ratio", "52 weeks", "Pearson correlation of weekly CAD returns."),
    "avg_corr_universe_1y": ("Average correlation with the other 30 assets, 1 year", "risk_adjusted", "ratio", "52 weeks",
                             "Mean pairwise correlation of weekly CAD returns."),
    "market_cap": ("Market capitalisation", "size_income", "cad", "latest", "Shares outstanding (latest SEC filing cover page) x last close, in CAD."),
    "dividend_yield_ttm": ("Dividend yield, trailing 12 months", "size_income", "fraction", "365 days", "Dividends with ex-date in the last 365 days / last close."),
    "dividend_growth_1y": ("Dividend growth, 1 year", "size_income", "fraction", "TTM vs prior TTM", "Trailing 12-month dividends vs the 12 months before."),
    "dividend_growth_3y": ("Dividend growth, 3 years (annual rate)", "size_income", "fraction", "TTM vs TTM 3 years ago", "Compound annual growth of trailing dividends."),
    "revenue_growth": ("Revenue growth", "fundamentals", "fraction", "latest fiscal year", "Annual revenue vs prior fiscal year (SEC XBRL)."),
    "earnings_growth": ("Earnings growth", "fundamentals", "fraction", "latest fiscal year", "Annual net income vs prior fiscal year (SEC XBRL)."),
    "roe": ("Return on equity", "fundamentals", "fraction", "latest fiscal year", "Net income / average shareholders' equity."),
    "roa": ("Return on assets", "fundamentals", "fraction", "latest fiscal year", "Net income / total assets."),
    "roic": ("Return on invested capital", "fundamentals", "fraction", "latest fiscal year",
             "Operating income x (1 - effective tax rate) / (equity + debt - cash)."),
    "pe": ("Price / earnings", "fundamentals", "ratio", "latest fiscal year", "Market cap / annual net income."),
    "pb": ("Price / book", "fundamentals", "ratio", "latest fiscal year", "Market cap / shareholders' equity."),
    "ev_ebitda": ("EV / EBITDA", "fundamentals", "ratio", "latest fiscal year",
                  "(Market cap + debt - cash) / (operating income + depreciation and amortisation)."),
    "debt_to_equity": ("Debt / equity", "fundamentals", "ratio", "latest fiscal year", "Total debt / shareholders' equity."),
    "net_debt_ebitda": ("Net debt / EBITDA", "fundamentals", "ratio", "latest fiscal year", "(Debt - cash) / EBITDA."),
    "fcf": ("Free cash flow", "fundamentals", "cad", "latest fiscal year", "Operating cash flow - capital expenditure, CAD."),
    "fcf_yield": ("Free cash flow yield", "fundamentals", "fraction", "latest fiscal year", "Free cash flow / market cap."),
    "payout_ratio": ("Payout ratio", "fundamentals", "fraction", "latest fiscal year", "Dividends paid / net income."),
    "factor_market": ("Factor exposure: market (SPY)", "factors", "ratio", "156 weeks", "Multiple regression of weekly CAD returns."),
    "factor_growth_tilt": ("Factor exposure: growth tilt (QQQ - SPY)", "factors", "ratio", "156 weeks", "Multiple regression of weekly CAD returns."),
    "factor_rates": ("Factor exposure: long rates (TLT)", "factors", "ratio", "156 weeks", "Multiple regression of weekly CAD returns."),
    "factor_gold": ("Factor exposure: gold (GLD)", "factors", "ratio", "156 weeks", "Multiple regression of weekly CAD returns."),
    "factor_usd": ("Factor exposure: USD/CAD", "factors", "ratio", "156 weeks", "Multiple regression of weekly CAD returns."),
    "factor_r2": ("Share of weekly moves explained by the factors (R squared)", "factors", "ratio", "156 weeks", "R squared of the factor regression."),
    "garch_vol_1m": ("GARCH(1,1) volatility forecast, next month", "forecast", "fraction", "fit on 756 trading days",
                     "Average annualised volatility expected over the next 21 trading days. A model estimate, not a measurement."),
    "garch_persistence": ("GARCH persistence (alpha + beta)", "forecast", "ratio", "fit on 756 trading days", "How slowly volatility shocks fade."),
    "garch_long_run_vol": ("GARCH long-run volatility", "forecast", "fraction", "fit on 756 trading days", "Volatility the model reverts to."),
    "p_turbulent": ("Probability of the turbulent volatility regime", "regime", "fraction", "fit on 756 trading days",
                    "Two-state Gaussian hidden Markov model on daily CAD returns; filtered probability on the last day."),
    "vol_calm_regime": ("Volatility in the calm regime", "regime", "fraction", "fit on 756 trading days", "Annualised volatility of the calm state."),
    "vol_turbulent_regime": ("Volatility in the turbulent regime", "regime", "fraction", "fit on 756 trading days", "Annualised volatility of the turbulent state."),
    "hurst_3y": ("Hurst exponent", "regime", "ratio", "756 trading days",
                 "Rescaled-range estimate on daily log returns: about 0.5 random walk, above 0.5 trending, below mean-reverting."),
    "news_count_30d": ("Articles in the last 30 days", "news", "count", "30 days", "Allow-listed outlets only (see sources)."),
    "news_count_90d": ("Articles in the last 90 days", "news", "count", "90 days", "Allow-listed outlets only."),
    "filings_90d": ("Regulatory filings in the last 90 days", "news", "count", "90 days", "SEC EDGAR 8-K, 10-K, 10-Q, 40-F, 20-F."),
    "news_sentiment_30d": ("News sentiment, 30 days", "news", "ratio", "30 days",
                           "Headline sentiment from -1 to +1, weighted by recency (14-day half-life)."),
    "news_sentiment_momentum": ("News sentiment momentum", "news", "ratio", "30 days vs days 31-90",
                                "Average sentiment of the last 30 days minus days 31 to 90."),
    "material_events_90d": ("Material events, 90 days", "news", "count", "90 days",
                            "Filings and articles classified as high materiality (e.g. 8-K items 1.01, 2.01, 5.02; lawsuits, probes, M&A)."),
    "negative_material_90d": ("Negative material events, 90 days", "news", "count", "90 days", "Material events with negative sentiment or type."),
    "esg_controversies_1y": ("ESG controversy items, 1 year", "news", "count", "365 days",
                             "Negative environmental, social or governance events in filings and allow-listed news."),
    "esg_controversy_level": ("ESG controversy evidence", "news", "label", "365 days",
                              "significant (a high-materiality item or 3+ items), minor, or none found in the sources searched."),
    "insider_buys_90d": ("Insider purchases, 90 days", "insider", "count", "90 days", "Open-market purchases (code P) on SEC Form 4."),
    "insider_sells_90d": ("Insider sales, 90 days", "insider", "count", "90 days", "Open-market sales (code S) on SEC Form 4."),
    "insider_net_shares_90d": ("Insider net shares bought, 90 days", "insider", "number", "90 days", "Purchases minus sales, shares."),
    "insider_net_value_90d": ("Insider net value bought, 90 days", "insider", "money_local", "90 days", "Purchases minus sales, listing currency."),
    "earnings_reaction_last": ("Price reaction to the last earnings release", "earnings", "fraction", "2 trading days",
                               "Asset minus SPY return around the 8-K Item 2.02 date. Proxy for an earnings surprise."),
    "earnings_reaction_avg4": ("Average reaction to the last 4 earnings releases", "earnings", "fraction", "2 trading days",
                               "Average of the abnormal 2-day reactions."),
}

UNAVAILABLE = {
    "forward_pe": "Needs analyst consensus estimates, which are not available from a public, redistributable source.",
    "analyst_revisions": "Needs analyst consensus estimate history (licensed data).",
    "earnings_surprise": "True surprises need consensus estimates; earnings_reaction_* is used as a market-based proxy.",
    "etf_holdings_exposure": "Fund holdings files are not ingested yet; funds are scored on their own prices and an "
                             "'unscreened index' flag.",
}

GROUP_LABELS = {
    "returns": "Returns", "risk": "Risk and drawdowns", "tail": "Tail risk", "risk_adjusted": "Risk-adjusted and market link",
    "relative": "Relative performance", "technical": "Technical indicators", "liquidity": "Liquidity",
    "size_income": "Size and income", "fundamentals": "Fundamentals", "factors": "Factor exposures",
    "forecast": "Volatility forecast", "regime": "Regimes and persistence", "news": "News and filings",
    "insider": "Insider transactions", "earnings": "Earnings reactions",
}


def _r(x, nd=4):
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return round(x, nd)


def _rets_rf(cad: list, rf: list, n: int | None) -> tuple[list[float], list[float]]:
    start = 0 if n is None else max(0, len(cad) - n - 1)
    rets, rfs, prev = [], [], None
    for i in range(start, len(cad)):
        v = cad[i]
        if v is None:
            continue
        if prev is not None:
            rets.append(v / prev - 1)
            rfs.append(rf[i])
        prev = v
    return rets, rfs


def _years(md: MarketData, i0: int, i1: int) -> float:
    return (date.fromisoformat(md.dates[i1]) - date.fromisoformat(md.dates[i0])).days / 365.25


def _value_on_or_before(md: MarketData, values: list, day: str):
    from bisect import bisect_right

    i = bisect_right(md.dates, day) - 1
    while i >= 0 and values[i] is None:
        i -= 1
    return (values[i], md.dates[i]) if i >= 0 else (None, None)


def _ttm_dividends(divs: list[tuple[str, float]], end: date, years_back: int = 0) -> float:
    e = date(end.year - years_back, end.month, min(end.day, 28))
    s = date(e.year - 1, e.month, e.day)
    return sum(v for d, v in divs if s.isoformat() < d <= e.isoformat())


def _big_moves(rows: list[dict], per_year: int = 6, top: int = 30) -> list[dict]:
    """Unusual daily moves: at least 3 times the trailing one-year standard deviation of daily
    returns. Up to `per_year` per calendar year (largest first, at least 5 rows apart, at most
    `top` in total), so every year of the window is covered for news lookups."""
    closes = [r["adj_close"] for r in rows]
    dates = [r["date"] for r in rows]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    cands = []
    for i in range(60, len(rets)):
        sd = statistics.pstdev(rets[max(0, i - 252):i])
        if sd <= 0:
            continue
        z = rets[i] / sd
        if abs(z) >= 3:
            cands.append((abs(z), i, z))
    chosen: list[tuple[int, float]] = []
    per: dict[str, int] = {}
    for _, i, z in sorted(cands, reverse=True):
        year = dates[i + 1][:4]
        if per.get(year, 0) >= per_year or not all(abs(i - j) > 5 for j, _ in chosen):
            continue
        chosen.append((i, z))
        per[year] = per.get(year, 0) + 1
        if len(chosen) == top:
            break
    return [{"date": dates[i + 1], "return": round(rets[i], 4), "z": round(z, 2)} for i, z in sorted(chosen)]


def _asset_features(t: str, md: MarketData, fund_doc: dict, sig_by: dict, as_of: str) -> dict:
    s = md.series[t]
    a = md.assets[t]
    cad, rf = s.cad, md.rf_daily
    f: dict = {}
    miss: dict = {}
    details: dict = {}
    if not s.rows:
        return {"values": {}, "missing": {"all": "No prices."}, "details": {}}

    for k, n in N.items():
        f[f"ret_{k}"] = ind.total_return(cad, n)
    for k in ("1m", "3m", "6m", "1y"):
        w = ind.window(cad, N[k])
        f[f"vol_{k}"] = ind.ann_vol(ind.returns(w)) if ind.covered(cad, N[k]) else None
    first_v, last_v, first_i = ind.first_last(cad)
    last_i = len(cad) - 1
    if ind.covered(cad, N["3y"]):
        i0 = last_i - N["3y"]
        f["cagr_3y"] = ind.cagr(cad[i0], cad[last_i], _years(md, i0, last_i))
    if first_i is not None:
        f["cagr_5y"] = ind.cagr(first_v, last_v, _years(md, first_i, last_i))
    for k, n in (("1y", N["1y"]), ("3y", N["3y"]), ("5y", None)):
        w = cad if n is None else ind.window(cad, n)
        mdd, info = ind.max_drawdown(w)
        f[f"mdd_{k}"] = mdd
        if k == "5y" and info:
            offset = len(cad) - len(w)
            peak_v = w[info["peak_index"]]
            rec = None
            for j in range(info["trough_index"] + 1, len(w)):
                if w[j] is not None and w[j] >= peak_v:
                    rec = j - info["trough_index"]
                    break
            f["recovery_days_5y"] = rec
            details["drawdown_5y"] = {"peak_date": md.dates[offset + info["peak_index"]],
                                      "trough_date": md.dates[offset + info["trough_index"]], "recovered_after_days": rec}
            if rec is None:
                miss["recovery_days_5y"] = "Not recovered: the price had not regained its prior peak by the end of the data."
    p0, d0 = _value_on_or_before(md, cad, STRESS_2022[0])
    p1, d1 = _value_on_or_before(md, cad, STRESS_2022[1])
    f["stress_2022"] = p1 / p0 - 1 if p0 and p1 and d0 and d0 >= (s.rows[0]["date"]) else None

    r1, rf1 = _rets_rf(cad, rf, N["1y"])
    r3, rf3 = _rets_rf(cad, rf, N["3y"])
    f["downside_vol_1y"] = ind.downside_dev(r1)
    f["sharpe_1y"], f["sortino_1y"] = ind.sharpe(r1, rf1), ind.sortino(r1, rf1)
    if ind.covered(cad, N["3y"]):
        f["sharpe_3y"], f["sortino_3y"] = ind.sharpe(r3, rf3), ind.sortino(r3, rf3)
        f["var_95_1d"], f["es_95_1d"] = ind.var_es(r3)
        g = ind.garch11(r3)
        if g:
            f["garch_vol_1m"], f["garch_persistence"], f["garch_long_run_vol"] = g["vol_forecast"], g["persistence"], g["vol_long_run"]
            details["garch"] = {k: _r(v) for k, v in g.items()}
        h = ind.hmm2(r3)
        if h:
            details["regime"] = {k: _r(v) if not isinstance(v, bool) else v for k, v in h.items()}
            if h["distinct"]:
                f["p_turbulent"], f["vol_calm_regime"], f["vol_turbulent_regime"] = h["p_turbulent"], h["vol_calm"], h["vol_turbulent"]
            else:
                miss["p_turbulent"] = "The two fitted regimes are not distinct (volatility ratio below 1.25)."
        f["hurst_3y"] = ind.hurst_rs([math.log1p(x) for x in r3])
    else:
        miss["sharpe_3y"] = "Less than 3 years of history."

    # Weekly returns: beta, correlation, factors.
    spy = md.series["SPY"].cad if "SPY" in md.series else None
    if spy:
        for k, weeks_start in (("1y", N["1y"]), ("3y", N["3y"])):
            i0 = max(0, len(md.dates) - weeks_start - 1)
            wa, wm = paired_returns(week_end_values(md.dates, cad, i0), week_end_values(md.dates, spy, i0))
            f[f"beta_{k}"] = ind.beta(wa, wm)
            if k == "1y":
                f["corr_spy_1y"] = ind.pearson(wa, wm)
        i0 = max(0, len(md.dates) - N["3y"] - 1)
        weekly = {x: week_end_values(md.dates, md.series[x].cad, i0) for x in ("SPY", "QQQ", "TLT", "GLD") if x in md.series}
        if len(weekly) == 4:
            mine = week_end_values(md.dates, cad, i0)
            fxw = week_end_values(md.dates, md.fx, i0)
            ys, cols = [], [[], [], [], [], []]
            for k2 in range(1, len(mine)):
                vals = [mine, weekly["SPY"], weekly["QQQ"], weekly["TLT"], weekly["GLD"], fxw]
                if any(v[k2] is None or v[k2 - 1] is None for v in vals):
                    continue
                rr = [v[k2] / v[k2 - 1] - 1 for v in vals]
                ys.append(rr[0])
                cols[0].append(rr[1]); cols[1].append(rr[2] - rr[1]); cols[2].append(rr[3]); cols[3].append(rr[4]); cols[4].append(rr[5])
            res = ind.ols(ys, cols)
            if res:
                for key, b in zip(("factor_market", "factor_growth_tilt", "factor_rates", "factor_gold", "factor_usd"), res["betas"]):
                    f[key] = b
                f["factor_r2"] = res["r2"]

    # Technicals on own rows, listing currency.
    closes = [r["adj_close"] for r in s.rows]
    for n in (20, 50, 200):
        v = ind.sma(closes, n)
        f[f"sma_{n}"] = v
        f[f"px_vs_sma_{n}"] = closes[-1] / v - 1 if v else None
    f["rsi_14"] = ind.rsi_wilder(closes[-300:], 14)
    m = ind.macd(closes[-400:])
    if m:
        f["macd"], f["macd_signal"] = m["macd"], m["signal"]
        f["macd_hist_pct"] = m["hist"] / closes[-1]
        details["macd_cross"] = ("bullish_cross" if m["hist"] > 0 >= m["hist_prev"] else
                                 "bearish_cross" if m["hist"] < 0 <= m["hist_prev"] else None)
    ohlc = [r for r in s.rows[-300:] if r.get("high") and r.get("low") and r.get("close")]
    if len(ohlc) >= 42:
        atr = ind.atr_wilder([r["high"] for r in ohlc], [r["low"] for r in ohlc], [r["close"] for r in ohlc], 14)
        f["atr_14"] = atr
        f["atr_pct"] = atr / ohlc[-1]["close"] if atr else None
    hi = max(closes[-252:])
    f["pct_from_52w_high"] = closes[-1] / hi - 1

    # Liquidity in CAD.
    from bisect import bisect_right
    def fx_at(d):
        i = bisect_right(md.dates, d) - 1
        return md.fx[max(i, 0)]
    dv = []
    for r in s.rows:
        vol = r.get("volume")
        if vol is None or r.get("close") is None:
            continue
        amount = vol if s.market == "crypto" else vol * r["close"]
        dv.append(amount * (fx_at(r["date"]) if s.currency == "USD" else 1.0))
    if dv:
        f["adv_30"] = statistics.fmean(dv[-30:])
        f["adv_90"] = statistics.fmean(dv[-90:])
    else:
        miss["adv_30"] = "No volume in the price files."

    # Income.
    end = date.fromisoformat(s.rows[-1]["date"])
    divs = md.dividends.get(t) or []
    last_close = s.rows[-1].get("close") or s.rows[-1]["adj_close"]
    ttm = _ttm_dividends(divs, end)
    f["dividend_yield_ttm"] = ttm / last_close if last_close else None
    ttm1, ttm3 = _ttm_dividends(divs, end, 1), _ttm_dividends(divs, end, 3)
    if ttm > 0 and ttm1 > 0:
        f["dividend_growth_1y"] = ttm / ttm1 - 1
    if ttm > 0 and ttm3 > 0 and divs and divs[0][0] <= date(end.year - 4, end.month, min(end.day, 28)).isoformat():
        f["dividend_growth_3y"] = (ttm / ttm3) ** (1 / 3) - 1
    elif ttm > 0:
        miss["dividend_growth_3y"] = "Needs four years of dividend history."

    # Market cap and fundamentals (equities with SEC filings).
    issuers = (fund_doc or {}).get("issuers") or {}
    issuer = issuers.get(t)
    to_cad = md.fx[-1] if issuer and issuer.get("currency") == "USD" else 1.0
    if a["asset_group"] == "equity":
        shares = (issuer or {}).get("shares_outstanding") or {}
        if shares.get("value"):
            px_cad = last_close * (md.fx[-1] if s.currency == "USD" else 1.0)
            f["market_cap"] = shares["value"] * px_cad
            details["market_cap"] = {"shares": shares["value"], "shares_as_of": shares.get("as_of"), "price_date": s.rows[-1]["date"]}
        fv, fm, fp = ratios(issuer, f.get("market_cap"), to_cad, a["sector"], as_of)
        f.update(fv)
        miss.update(fm)
        if fp:
            details["fundamentals"] = fp
        if not issuer:
            reason = ((fund_doc or {}).get("not_covered") or {}).get(t) or \
                "Fundamentals not loaded: run the SEC EDGAR ingest job (python -m alphaforge.ingest.run --jobs sec_edgar)."
        else:
            reason = fm.get("fundamentals") or "An input was not reported in the XBRL data for the latest fiscal year."
        for k in FUNDAMENTAL_KEYS:
            if f.get(k) is None:
                miss.setdefault(k, reason if k != "market_cap" or not issuer else
                                "Shares outstanding not found in the issuer's recent SEC filings.")
    else:
        why = "Not applicable to a fund." if a["asset_group"] in ("broad_equity_fund", "bond", "gold") else "Not applicable to a crypto asset."
        for k in FUNDAMENTAL_KEYS:
            miss[k] = why

    # News, insider, earnings.
    items = sig_by.get(t, [])
    nv, nm, nd = news_features(items, as_of)
    f.update(nv); miss.update(nm); details["news"] = nd
    iv, im = insider_features(((fund_doc or {}).get("insider") or {}).get(t))
    f.update(iv)
    if a["asset_group"] == "equity":
        miss.update(im)
    if a["asset_group"] == "equity" and "SPY" in md.series:
        own = {r["date"]: r["adj_close"] for r in s.rows}
        spy_own = {r["date"]: r["adj_close"] for r in md.series["SPY"].rows}
        ev, em, er = earnings_reactions(items, own, spy_own)
        f.update(ev); miss.update(em)
        if er:
            details["earnings_reactions"] = er

    details["big_moves"] = _big_moves(s.rows)
    details["history"] = {"first_date": s.rows[0]["date"], "last_date": s.rows[-1]["date"], "rows": len(s.rows),
                          "live_rows": s.live_rows, "carried_days": s.carried}
    values = {k: (_r(v) if k not in ("market_cap", "fcf", "adv_30", "adv_90", "insider_net_value_90d") else _r(v, 0))
              for k, v in f.items() if v is not None}
    for k, v in f.items():
        if v is None and k not in miss:
            miss[k] = "Not enough history in the window."
    return {"values": values, "missing": miss, "details": details}


def _relative(features: dict, md: MarketData) -> None:
    by_sector: dict[str, list[str]] = {}
    for t, a in md.assets.items():
        if a["asset_group"] == "equity":
            by_sector.setdefault(a["sector"], []).append(t)
    for t, a in md.assets.items():
        v = features[t]["values"]
        r = v.get("ret_1y")
        if r is None:
            continue
        home = "XIU.TO" if t.endswith(".TO") else "SPY"
        hr = features.get(home, {}).get("values", {}).get("ret_1y")
        if hr is not None and t != home:
            v["rel_market_1y"] = _r(r - hr)
        if a["asset_group"] == "equity":
            peers = [p for p in by_sector.get(a["sector"], []) if p != t and features[p]["values"].get("ret_1y") is not None]
            if peers:
                v["rel_sector_1y"] = _r(r - statistics.fmean(features[p]["values"]["ret_1y"] for p in peers))
                features[t]["details"]["sector_peers"] = peers
            else:
                features[t]["missing"]["rel_sector_1y"] = f"No other {a['sector']} stock in the universe."


def _correlation_1y(md: MarketData) -> dict:
    i0 = max(0, len(md.dates) - N["1y"] - 1)
    tickers = list(md.assets)
    weekly = {t: week_end_values(md.dates, md.series[t].cad, i0) for t in tickers}
    matrix = []
    for a in tickers:
        row = []
        for b in tickers:
            row.append(1.0 if a == b else _r(ind.pearson(*paired_returns(weekly[a], weekly[b]))))
        matrix.append(row)
    return {"tickers": tickers, "matrix": matrix, "start": md.dates[i0], "end": md.dates[-1], "frequency": "weekly"}


def _macro_at(md: MarketData, sid: str, day: str):
    pts = md.macro.get(sid) or []
    val = None
    for d, v in pts:
        if d <= day:
            val = (d, v)
        else:
            break
    return val


def market_regime(md: MarketData, features: dict) -> dict:
    as_of = md.dates[-1]
    eq = {}
    for t in ("SPY", "XIU.TO"):
        v = features.get(t, {}).get("values", {})
        eq[t] = {k: v.get(k) for k in ("px_vs_sma_200", "pct_from_52w_high", "p_turbulent", "vol_1m", "vol_1y", "ret_3m", "rsi_14")}
    spy = eq["SPY"]
    dd = spy.get("pct_from_52w_high")
    pt = spy.get("p_turbulent")
    trend = spy.get("px_vs_sma_200")
    rules = [
        "Stress: S&P 500 (SPY) more than 20% below its 52-week high.",
        "Correction: SPY 10% to 20% below its 52-week high.",
        "Volatile: probability of SPY's turbulent volatility regime above 60%.",
        "Calm uptrend: SPY above its 200-day average and turbulent-regime probability below 40%.",
        "Otherwise: mixed.",
    ]
    if dd is not None and dd < -0.20:
        label = "stress"
    elif dd is not None and dd < -0.10:
        label = "correction"
    elif pt is not None and pt > 0.60:
        label = "volatile"
    elif trend is not None and trend > 0 and (pt is None or pt < 0.40):
        label = "calm_uptrend"
    else:
        label = "mixed"
    rates = {}
    for sid, key in (("V39079", "ca_policy_rate"), ("DFF", "us_fed_funds"), ("BD.CDN.10YR.DQ.YLD", "ca_10y"),
                     ("BD.CDN.2YR.DQ.YLD", "ca_2y"), ("DGS10", "us_10y"), ("DGS2", "us_2y")):
        m = _macro_at(md, sid, as_of)
        if m:
            rates[key] = {"value": m[1], "date": m[0], "series_id": sid}
    if "ca_10y" in rates and "ca_2y" in rates:
        rates["ca_curve_10y_2y"] = round(rates["ca_10y"]["value"] - rates["ca_2y"]["value"], 2)
    if "us_10y" in rates and "us_2y" in rates:
        rates["us_curve_10y_2y"] = round(rates["us_10y"]["value"] - rates["us_2y"]["value"], 2)
    past = _macro_at(md, "V39079", (date.fromisoformat(as_of) - timedelta(days=182)).isoformat())
    if past and "ca_policy_rate" in rates:
        change = round(rates["ca_policy_rate"]["value"] - past[1], 2)
        rates["ca_policy_change_6m"] = change
        rates["ca_policy_direction"] = "cutting" if change < 0 else "hiking" if change > 0 else "on hold"
    text = {
        "stress": "Equity markets are in a stress phase: the S&P 500 fund is more than 20% below its 52-week high.",
        "correction": "Equity markets are in a correction: the S&P 500 fund is 10% to 20% below its 52-week high.",
        "volatile": "Equity markets are in a turbulent volatility regime according to the regime model.",
        "calm_uptrend": "Equity markets are in a calm uptrend: the S&P 500 fund is above its 200-day average with low turbulence.",
        "mixed": "Equity market signals are mixed.",
    }[label]
    return {"as_of": as_of, "label": label, "summary": text, "equity": {k: {kk: _r(vv) for kk, vv in v.items()} for k, v in eq.items()},
            "rates": rates, "rules": rules,
            "note": "A regime label describes current conditions from past prices; it does not forecast markets."}


def build_features(read, generated_at: str | None = None) -> dict:
    md = load_market_data(read)
    as_of = md.dates[-1]
    fund_doc = read("fundamentals.json") or {}
    sig_doc = read("signals.json") or {}
    sig_by = by_ticker(sig_doc.get("signals") or [], as_of)
    features = {t: _asset_features(t, md, fund_doc, sig_by, as_of) for t in md.assets}
    _relative(features, md)
    corr = _correlation_1y(md)
    idx = {t: i for i, t in enumerate(corr["tickers"])}
    for t in md.assets:
        row = [c for j, c in enumerate(corr["matrix"][idx[t]]) if j != idx[t] and c is not None]
        if row:
            features[t]["values"]["avg_corr_universe_1y"] = _r(statistics.fmean(row))
    regime = market_regime(md, features)
    live = md.sources.get("live_extension")
    from ..ingest.news import macro_events  # standard library only; same ids as the ingest job's events

    policy = macro_events({"series": {sid: {"label": md.macro_meta.get(sid, {}).get("label"), "points": pts}
                                      for sid, pts in md.macro.items() if sid in ("V39079", "DFF")}},
                          generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {
            "kind": "pipeline",
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "transform_version": TRANSFORM_VERSION,
            "description": "Market features in CAD from adj_close on the union of TSX and U.S. trading days (technical "
                           "indicators in listing currency on each asset's own rows), plus fundamentals from SEC EDGAR "
                           "XBRL, news and filing features from signals.json, and insider data from SEC Form 4.",
            "source_files": [f for f in ("prices/*.json", "fx_usdcad.json", "macro_series.json", "dividends.json",
                                         "live_extension.json", "fundamentals.json", "signals.json")
                             if f == "prices/*.json" or read(f) is not None],
        },
        "as_of": as_of,
        "base_currency": "CAD",
        "data_through": {"starter": md.starter_end, "live": md.live_end},
        "live_extension": live,
        "definitions": {k: {"label": v[0], "group": v[1], "unit": v[2], "window": v[3], "method": v[4]} for k, v in DEFINITIONS.items()},
        "groups": GROUP_LABELS,
        "unavailable": UNAVAILABLE,
        "assets": features,
        "correlation_1y": corr,
        "market_regime": regime,
        "policy_decisions": policy,
        "signals_used": len(sig_doc.get("signals") or []),
    }


def dir_reader(path: Path):
    def read(name: str):
        p = path / name
        if "*" in name:
            return None
        return json.loads(p.read_text()) if p.exists() else None
    return read


def main() -> None:
    ap = argparse.ArgumentParser(description="Build asset_features.json in a curated bundle folder.")
    ap.add_argument("--bundle", type=Path, required=True)
    args = ap.parse_args()
    doc = build_features(dir_reader(args.bundle))
    (args.bundle / "asset_features.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"Wrote asset_features.json for {len(doc['assets'])} assets, as of {doc['as_of']}")


if __name__ == "__main__":
    main()
