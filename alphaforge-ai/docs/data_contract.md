# Data contract: how the starter CSVs plug in

The scoring, radar, suggestion and governance engines never read raw CSVs. They read a **curated bundle** of JSON files from one place:

| `ALPHAFORGE_DATA_SOURCE` | Location |
|---|---|
| `fixture` (default) | `backend/alphaforge/data/fixtures/` (placeholder values) |
| `dir:/abs/path` | a local folder written by `data_pipeline` |
| `s3://bucket/prefix` | the same files in S3 (used by Lambda) |

To switch from placeholders to real figures, `data_pipeline` writes the files below and the setting points to them. No backend code changes. The UI's placeholder banner disappears once `asset_metrics.json` has `provenance.kind = "pipeline"`.

## Files

| File | Required | Produced from |
|---|---|---|
| `assets.json` | yes | `assets_universe.csv` + team-assigned tags, region, ESG flags |
| `asset_metrics.json` | yes | `prices_daily.csv`, `fx_usdcad_daily.csv`, `corporate_actions.csv`, `macro_rates.csv` (risk-free rate) |
| `correlations.json` | no; without it, correlations use documented group priors and confidence drops | `prices_daily.csv` + FX |
| `signals.json` | no (on AWS, signals will come from the monitoring pipeline) | ingest + enrichment |
| `macro_snapshot.json` | no | `macro_rates.csv` with publication lags |
| `data_quality_report.json` | no | validation step |
| `prices/<TICKER>.json` | no; enables the price chart and the feature engine | `prices_daily.csv` |
| `fx_usdcad.json`, `macro_series.json`, `dividends.json` | no; inputs of the feature engine | `fx_usdcad_daily.csv`, `macro_rates.csv`, `corporate_actions.csv` |
| `asset_features.json` | no; without it the fit score falls back to window metrics and confidence drops | feature engine (`alphaforge.features`) |
| `live_extension.json` | no | ingest job `market_data` |
| `fundamentals.json` | no | ingest job `sec_edgar` |
| `ingest_status.json`, `news_backfill_state.json` | no | ingest jobs |

The offline pipeline (`data_pipeline/build_bundle.py`) writes everything up to `asset_features.json` from the starter CSVs alone. The ingest jobs (`python -m alphaforge.ingest.run`, or the `alphaforge-ingest` Lambda) add `live_extension.json`, `fundamentals.json` and `signals.json`, then rebuild `asset_features.json`. Re-running the pipeline keeps the ingest files and uses them.

### `assets.json`

```json
{"schema_version": "1.0",
 "provenance": {"kind": "pipeline", "generated_at": "YYYY-MM-DD", "description": "...", "source_files": ["..."]},
 "assets": [{
   "ticker": "ENB.TO", "name": "Enbridge", "asset_class": "equity",
   "asset_group": "equity | broad_equity_fund | bond | gold | crypto",
   "region": "CA | US | Global", "listing_currency": "CAD | USD", "sector": "Energy",
   "is_defensive": false, "is_broad_fund": false,
   "tags": ["established", "dividend", "energy"],
   "esg": {"flags": ["fossil_fuels"], "notes": "source and reasoning"}
 }]}
```

Tags in use: `established, dividend, growth, tech, ai, emerging_tech, high_profile, crypto, esg, broad_market, bonds, short_term, long_duration, gold, real_assets, bank, insurance, energy, pipelines, rail, utility, consumer_staples, healthcare, semiconductors, cloud, ecommerce, software, ev`. ESG flags: `fossil_fuels` (screenable) and `unscreened_index` (index fund without an ESG screen, counted at 50% on the ESG axis). The current values are hand-assigned; document each source when ESG evidence is added.

### `asset_metrics.json`

```json
{"schema_version": "1.0",
 "provenance": {"kind": "pipeline", "generated_at": "...", "source_files": ["..."], "transform_version": "git sha"},
 "base_currency": "CAD",
 "stress_scenarios": {"2022_rates_shock": {"label": "...", "start": "2022-01-03", "end": "2022-12-30", "description": "..."}},
 "windows": {
   "3y": {"start": "2023-11-13", "end": "2026-11-12", "metrics": {
     "SPY": {
       "ann_return": 0.14, "ann_vol": 0.17, "max_drawdown": -0.22,
       "downside_dev": null, "cvar_95": null, "worst_month": null, "recovery_days": null,
       "beta_spy": 1.0, "corr_spy": 1.0, "dividend_yield_ttm": 0.012, "median_dollar_volume": null,
       "history_days": 750, "data_gaps": 0,
       "stress": {"2022_rates_shock": -0.13}
     }}}}}
```

Required per ticker: `ann_vol`, `max_drawdown`. All others may be `null`, and the engines then say the value is missing. Fractions, not percentages (0.17 = 17%). The default window is `3y` (`ALPHAFORGE_DEFAULT_WINDOW`). If a window is missing, the engines fall back to the first available one and report it.

### `correlations.json`

```json
{"windows": {"3y": {"tickers": ["AAPL", "AGG"], "matrix": [[1.0, 0.12], [0.12, 1.0]]}}}
```

### `signals.json`

Filings, official statements, policy-rate decisions and headlines. Headline, publisher, date and link only; no article text is stored.

```json
{"provenance": {"kind": "ingest", "counts_by_source": {"sec_edgar": 950, "google_news": 900, "official_rss": 25, "dataset": 38}},
 "signals": [{
  "signal_id": "sha1 prefix", "kind": "news | filing | official | macro",
  "source_id": "gdelt | google_news | sec_edgar | official_rss | dataset", "source_tier": "major_media | official",
  "publisher": "Reuters", "domain": "reuters.com", "published_at": "2026-11-10T14:00:00Z", "retrieved_at": "...",
  "title": "...", "summary": "", "source_url": "https://...", "discovered_via": "gdelt | google_news",
  "tickers": ["XOM"], "sectors": ["Energy"],
  "event_type": "earnings | mna | lawsuit | regulatory_action | investigation | environmental_incident | ...",
  "esg_category": "environmental | social | governance | null", "materiality": "high | medium | low",
  "sentiment": -0.6, "sentiment_method": "lexicon | amazon_comprehend | filing_type", "severity": 4,
  "classification_rule": "matched 'class action'",
  "form": "8-K", "items": ["2.02"],
  "linked_move": {"ticker": "XOM", "date": "2026-11-04", "return": -0.07, "z": -3.4}}]}
```

Rules: a news item is kept only if its publisher is on the allow-list in `alphaforge/ingest/sources.py` and the headline names the company; newsletter-style headlines are dropped. 8-K items map to event types and materiality (`alphaforge/ingest/enrich.py`, `EIGHT_K_ITEMS`). `linked_move` marks articles found by the backfill around an unusual price move.

### `asset_features.json`

```json
{"as_of": "2026-11-13", "base_currency": "CAD", "data_through": {"starter": "2026-11-12", "live": "2026-11-15"},
 "definitions": {"vol_1y": {"label": "Volatility, 1 year", "group": "risk", "unit": "fraction", "window": "252 trading days", "method": "..."}},
 "groups": {"risk": "Risk and drawdowns"}, "unavailable": {"forward_pe": "reason"},
 "assets": {"TSLA": {"values": {"vol_1y": 0.4498, "...": 0},
                     "missing": {"pe": "reason the value is missing"},
                     "details": {"big_moves": [{"date": "2024-10-24", "return": 0.2192, "z": 6.43}],
                                 "drawdown_5y": {"peak_date": "...", "trough_date": "...", "recovered_after_days": 494},
                                 "garch": {}, "regime": {}, "fundamentals": {"fiscal_year": "FY2025", "form": "10-K", "filed": "..."},
                                 "news": {"latest": [], "controversies": [], "material": []}, "earnings_reactions": []}}},
 "correlation_1y": {"tickers": [], "matrix": []},
 "market_regime": {"label": "calm_uptrend", "summary": "...", "equity": {}, "rates": {}, "rules": []},
 "policy_decisions": [{"kind": "macro", "title": "Bank of Canada raised its policy rate from 2.25% to 2.50%"}]}
```

Feature families (definitions and units are in the file): returns 1M/3M/6M/1Y/3Y and CAGR 3Y/5Y; volatility 1M/3M/6M/1Y and downside volatility; maximum drawdown 1Y/3Y/5Y and recovery time; calendar-2022 return; VaR and expected shortfall (CVaR) 95%; beta 1Y/3Y; Sharpe and Sortino 1Y/3Y (risk-free: Bank of Canada policy rate); RSI 14; SMA 20/50/200; MACD (12, 26, 9); ATR 14; average dollar volume 30/90 days; 1-year correlation matrix; market cap; dividend yield and growth; revenue and earnings growth, ROE, ROA, ROIC, P/E, P/B, EV/EBITDA, debt/equity, net debt/EBITDA, FCF and FCF yield, payout ratio (SEC XBRL, point in time); 1-year return vs sector peers and home market; factor exposures (market, growth tilt, long rates, gold, USD/CAD); GARCH(1,1) next-month volatility; two-state regime probability; Hurst exponent; news counts, sentiment, sentiment momentum, material events and ESG controversy level; insider trades (Form 4); price reaction to earnings releases (proxy for surprises).

Not computed, with the reason in `unavailable`: forward P/E, analyst estimate revisions, true earnings surprises (need consensus estimates) and ETF holdings look-through.

### `live_extension.json`

Rows after the starter window. `adj_close` is spliced: the live series is rescaled so it equals the starter `adj_close` on the last shared date (`splice.anchor_date`, `splice.factor`); a split after that date stops the splice for that ticker.

```json
{"starter_end": "2026-11-12", "through": "2026-11-15", "fetched_at": "...",
 "prices": {"SPY": {"currency": "USD", "splice": {"anchor_date": "2026-11-12", "factor": 1.0}, "points": [{"date": "2026-11-13", "adj_close": 754.37, "open": 760.07, "high": 760.12, "low": 747.88, "close": 754.37, "volume": 38391200}]}},
 "dividends": {"RY.TO": [["2026-11-20", 1.64]]}, "fx": [["2026-11-13", 1.4257]], "macro": {"DGS10": [["2026-11-12", 5.1]]},
 "sources": {}, "errors": []}
```

### `fundamentals.json`

```json
{"issuers": {"AAPL": {"cik": "0000320193", "taxonomy": "us-gaap", "currency": "USD",
   "fiscal_years": {"FY2026": {"end": "2026-09-26", "filed": "2026-11-03", "form": "10-K", "revenue": 0, "net_income": 0,
                               "equity": 0, "assets": 0, "operating_income": 0, "da": 0, "cfo": 0, "capex": 0, "debt": 0,
                               "cash": 0, "dividends_paid": 0}},
   "shares_outstanding": {"value": 0, "as_of": "...", "concept": "dei:EntityCommonStockSharesOutstanding"}}},
 "insider": {"AAPL": {"insider_buys_90d": 0, "insider_sells_90d": 32, "insider_net_shares_90d": -291172, "insider_net_value_90d": -97039854}},
 "not_covered": {"ATD.TO": "reason"}}
```

Each annual value is the first one filed for that period end (point in time), so later restatements do not leak into earlier dates.

### `prices/<TICKER>.json`

Built by `python3 data_pipeline/build_prices.py` (standard library only). By default it writes into the fixture bundle (`backend/alphaforge/data/fixtures/prices/`); pass `--out` for another bundle. Prices stay in listing currency, one point per reported row, with no forward-fill or FX conversion.

```json
{"schema_version": "1.0", "ticker": "SPY", "currency": "USD",
 "provenance": {"kind": "pipeline", "generated_at": "YYYY-MM-DD", "source_files": ["alphaforge_starter/prices_daily.csv"],
                "source_sha256": "...", "transform_version": "build_prices 1.1", "dropped_rows": 0, "ohlc_missing_rows": 0},
 "points": [{"date": "2021-11-12", "adj_close": 437.0388, "close": 467.27, "open": 465.12, "high": 467.86, "low": 464.11}]}
```

`adj_close` drives the line chart. `open`, `high`, `low` and `close` are traded prices (split-adjusted, not dividend-adjusted) and drive the candle chart. They are `null` on a row where they are missing or inconsistent (high below open or close, low above them); candles skip that day. Weekly (ISO weeks) and monthly candles are aggregated in the browser: first open, highest high, lowest low, last close.

The API serves the full series in `GET /assets/{ticker}` (`price_history`) and a downsampled version for the Assets table in `GET /prices/sparklines`: at most 80 `adj_close` values per asset at evenly spaced positions, always keeping the first and last observation.

## Processing rules the pipeline must follow

These come from `alphaforge_starter/README.md` and the brief.

1. **Returns from `adj_close` only.** `close` is split-adjusted only. Never re-apply splits from `corporate_actions.csv`.
2. **CAD base.** USD assets are converted with Bank of Canada USD/CAD (`fx_usdcad_daily.csv`), carried forward over Canadian holidays and flagged when carried.
3. **Calendar alignment.** Use the union of TSX and NYSE trading days. Carry equity prices forward for at most 3 days across the other market's holidays, with a flag. Sample crypto on those days, so weekend moves land in Monday's return. Document the choice.
4. **No look-ahead.** Monthly CPI and unemployment are dated by reference month. Add an `available_date` (about 15 days after the month for CPI, first Friday of the next month for U.S. unemployment) and never use a value before that date. FRED values are revised, not first-release.
5. **Known anomalies.** Flag ZST.TO's zero-volume row on 2022-07-06 and CNQ.TO's 1.021 "split" on 2022-08-22, which is likely a special distribution. Verify CNQ before relying on it.
6. **Survivorship bias.** State it wherever results are shown: the universe was chosen in 2026.
7. **Lineage.** Every output carries `source_files`, `generated_at` and `transform_version`.

## Sanity checks for the pipeline tests

`data_pipeline/build_bundle.py` measures calendar 2022 from the last close of 2021 (2021-12-31) to the last close of 2022 (2022-12-30). It gives, **in CAD**: SPY −12.6%, TLT −26.5%, XBB.TO −11.7%, GLD +6.0%. In **USD**, SPY and TLT lost 18.2% and 31.2%; the gap is the 2022 USD/CAD move (+6.8%). A test that expects the USD figures from CAD returns will fail, so `data_pipeline/tests` checks each currency against its own expected value.

The placeholder run started from the first trading day of 2022 (2022-01-03) instead, so it missed that day's move and gave TLT −24% and GLD +7%. The pipeline figures are the calendar-year returns.

## The pipeline (built)

```bash
python3 data_pipeline/build_bundle.py            # writes data_pipeline/out/ (standard library only)
backend/.venv/bin/python -m pytest data_pipeline/tests
ALPHAFORGE_DATA_SOURCE=dir:$PWD/data_pipeline/out backend/.venv/bin/python -m alphaforge.devserver   # run from backend/ with an absolute path
```

Choices it makes, all recorded in the output files:

- **Calendar:** union of TSX and U.S. trading days (a market's day is one where at least half its assets report a price). Equities missing on the other market's holidays carry their last price for at most 3 days, flagged; crypto is sampled on the same days.
- **Windows:** `3y` from 2023-11-13 and `5y` from 2021-11-12, both to the last trading day (2026-11-12). The stress scenario is the same in both windows.
- **Metrics:** `ann_return` is the annualised compound return. `ann_vol`, `downside_dev` and `cvar_95` (mean of the worst 5% days, a daily figure) use daily simple returns, annualised with the observed trading days per year. `worst_month` uses complete calendar months. `recovery_days` counts trading days from the max-drawdown trough back to the prior peak (`null` if not recovered). `beta_spy`, `corr_spy` and `correlations.json` use weekly returns, which limits the effect of different closing times and holidays. `dividend_yield_ttm` is dividends with an ex-date in the last 365 days over the last close; assets without dividend rows get 0. `median_dollar_volume` is in CAD (Yahoo reports crypto volume in USD, equity volume in shares). Each asset also gets `carried_days`.
- **Macro snapshot:** each series' latest value already published on the as-of date. Lags: Bank of Canada series same day; FRED daily series 1 day; U.S. CPI 15 days and Canada CPI 21 days after the reference month; U.S. unemployment on the first Friday of the next month.
- **`assets.json`** is copied from the fixture after its names, classes, currencies and sectors are checked against `assets_universe.csv`.
