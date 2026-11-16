"""Checks on the curated bundle built from the starter CSVs (docs/data_contract.md)."""

import csv
import json
import math
from datetime import date

import pytest

from conftest import STARTER

FILES = ("assets.json", "asset_metrics.json", "correlations.json", "macro_snapshot.json", "data_quality_report.json")


def _load(bundle_dir, name):
    return json.loads((bundle_dir / name).read_text())


def _prices(ticker):
    with (STARTER / "prices_daily.csv").open() as f:
        return {r["date"]: r for r in csv.DictReader(f) if r["ticker"] == ticker}


def _fx():
    with (STARTER / "fx_usdcad_daily.csv").open() as f:
        return {r["date"]: float(r["usdcad"]) for r in csv.DictReader(f)}


# ----- contract and lineage ---------------------------------------------------
def test_bundle_loads_as_pipeline_data(real_provider):
    st = real_provider.status()
    assert st["is_placeholder"] is False
    # Written by the ingest jobs (network), not by the offline pipeline.
    assert st["files_missing"] == ["signals.json", "fundamentals.json", "live_extension.json", "ingest_status.json"]
    assert "asset_features.json" in st["files_present"]
    assert st["asset_count"] == 31
    assert real_provider.windows == ["3y", "5y"]
    assert real_provider.window_period("3y") == "2023-11-13 to 2026-11-12"
    assert real_provider.window_period("5y") == "2021-11-12 to 2026-11-12"
    assert real_provider.resolve_window(None) == ("3y", None)  # default window exists: no fallback note


def test_every_file_carries_lineage(bundle_dir):
    for name in FILES[1:]:
        p = _load(bundle_dir, name)["provenance"]
        assert p["kind"] == "pipeline" and p["generated_at"] and p["transform_version"].startswith("build_bundle")
        assert p["source_files"] and set(p["source_sha256"]) == set(p["source_files"])
    assert len(list((bundle_dir / "prices").glob("*.json"))) == 31


def test_required_metrics_present_for_every_asset_and_window(real_provider):
    for w in real_provider.windows:
        for t in real_provider.assets:
            m = real_provider.metrics(t, w)
            assert m["ann_vol"] > 0 and m["max_drawdown"] <= 0
            assert m["history_days"] >= 750  # below 750 the engines lower confidence
            assert m["data_gaps"] == 0
            assert m["stress"]["2022_rates_shock"] is not None
            assert m["dividend_yield_ttm"] is not None and m["dividend_yield_ttm"] >= 0


# ----- sanity figures (contract) -------------------------------------------------
@pytest.mark.parametrize("ticker,cad", [("SPY", -0.126), ("TLT", -0.265), ("XBB.TO", -0.117), ("GLD", 0.060)])
def test_calendar_2022_returns_in_cad(real_provider, ticker, cad):
    for w in ("3y", "5y"):  # the scenario does not depend on the window
        assert real_provider.metrics(ticker, w)["stress"]["2022_rates_shock"] == pytest.approx(cad, abs=0.002)


@pytest.mark.parametrize("ticker,usd", [("SPY", -0.182), ("TLT", -0.312)])
def test_calendar_2022_returns_in_usd_are_checked_separately(bundle_dir, ticker, usd):
    values = _load(bundle_dir, "data_quality_report.json")["sanity_checks"]["values"][ticker]
    assert values["calendar_2022_return_local"] == pytest.approx(usd, abs=0.002)
    fx = _fx()
    # CAD and USD returns differ exactly by the 2022 USD/CAD move.
    fx_move = fx["2022-12-30"] / fx["2021-12-31"]
    assert 1 + values["calendar_2022_return_cad"] == pytest.approx((1 + usd) * fx_move, abs=0.003)


# ----- independent recomputation from the raw CSVs ----------------------------------
def test_ann_return_matches_raw_prices_and_fx(real_provider):
    rows, fx = _prices("SPY"), _fx()
    start, end = "2021-11-12", "2026-11-12"
    growth = float(rows[end]["adj_close"]) * fx[end] / (float(rows[start]["adj_close"]) * fx[start])
    years = (date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25
    assert real_provider.metric("SPY", "5y", "ann_return") == pytest.approx(growth ** (1 / years) - 1, abs=6e-5)


def test_max_drawdown_of_a_cad_asset_matches_its_own_rows(real_provider):
    # Carrying a CAD price forward repeats a value, so it cannot change the drawdown.
    rows = _prices("XIU.TO")
    peak, mdd = 0.0, 0.0
    for d in sorted(rows):
        v = float(rows[d]["adj_close"])
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    assert real_provider.metric("XIU.TO", "5y", "max_drawdown") == pytest.approx(mdd, abs=6e-5)


def test_dividend_yield_is_trailing_12_months_over_last_close(real_provider):
    with (STARTER / "corporate_actions.csv").open() as f:
        divs = [r for r in csv.DictReader(f) if r["ticker"] == "RY.TO" and r["action"] == "dividend"
                and "2025-11-12" < r["ex_date"] <= "2026-11-12"]
    assert len(divs) == 4
    close = float(_prices("RY.TO")["2026-11-12"]["close"])
    expected = sum(float(r["value"]) for r in divs) / close
    assert real_provider.metric("RY.TO", "3y", "dividend_yield_ttm") == pytest.approx(expected, abs=6e-5)
    assert real_provider.metric("BTC-USD", "3y", "dividend_yield_ttm") == 0.0


def test_correlation_matrix_is_valid_and_replaces_priors(bundle_dir, real_provider):
    for w, block in _load(bundle_dir, "correlations.json")["windows"].items():
        m, n = block["matrix"], len(block["tickers"])
        assert block["tickers"] == list(real_provider.assets)
        assert all(m[i][i] == 1.0 for i in range(n))
        assert all(m[i][j] == m[j][i] and -1 <= m[i][j] <= 1 for i in range(n) for j in range(n))
        # Positive definite (Cholesky succeeds), so portfolio variance can never be negative.
        low = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1):
                s = m[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
                if i == j:
                    assert s > 0
                    low[i][i] = math.sqrt(s)
                else:
                    low[i][j] = s / low[j][j]
        c, heuristic = real_provider.correlation("QQQ", "NVDA", w)
        assert heuristic is False and c == m[block["tickers"].index("QQQ")][block["tickers"].index("NVDA")]
        assert real_provider.metric("QQQ", w, "corr_spy") == m[block["tickers"].index("QQQ")][block["tickers"].index("SPY")]


# ----- calendar, FX, anomalies, macro ----------------------------------------------
def test_calendar_and_quality_report(bundle_dir):
    q = _load(bundle_dir, "data_quality_report.json")
    cal = q["calendar"]
    assert cal["union_days"] == cal["tsx_days"] + cal["us_only_days"]
    assert cal["max_carry_days"] == 3
    assert q["fx"]["union_days_with_carried_rate"] > 0  # Canadian holidays on which U.S. markets traded
    assert q["per_asset"]["ZST.TO"]["zero_volume_rows"] == ["2022-07-06"]
    assert {a["ticker"] for a in q["known_anomalies"]} == {"ZST.TO", "CNQ.TO"}
    assert q["per_asset"]["BTC-USD"]["carried_days"] == 0  # crypto trades every day
    assert any("Survivorship" in s for s in q["limitations"])


def test_macro_snapshot_has_no_look_ahead(bundle_dir):
    snap = _load(bundle_dir, "macro_snapshot.json")
    as_of = snap["as_of"]
    by_id = {s["series_id"]: s for s in snap["series"]}
    for s in snap["series"]:
        assert s["reference_date"] <= s["available_date"] <= as_of
    # October 2026 U.S. CPI exists in the CSV but is published after 2026-11-12, so September is used.
    assert by_id["CPIAUCSL"]["reference_date"] == "2026-09-01"
    # October unemployment comes out on the first Friday of November (2026-11-06).
    assert by_id["UNRATE"]["reference_date"] == "2026-10-01" and by_id["UNRATE"]["available_date"] == "2026-11-06"


# ----- the brief's persona behaviour on real data ------------------------------------
@pytest.fixture
def persona(real_provider):
    from alphaforge.profiles import preset_profile

    return lambda pid: preset_profile(pid, set(real_provider.assets))


def _by_ticker(result):
    return {r["ticker"]: r for r in result["results"]}


def test_robert_tsla_request_is_challenged(real_provider, persona):
    from alphaforge.governance import check_proposal

    res = check_proposal(persona("robert"), {"type": "add", "ticker": "TSLA", "weight": 0.30}, real_provider)
    # Measured correlations make TSLA a diversifier for Robert's bond-heavy portfolio, so its fit (34) is
    # higher than with the placeholder priors (27), but it is still red and outside his risk profile.
    assert res["status"] == "conflict" and res["asset_fit"]["score"] < 45
    assert res["asset_fit"]["status"] == "caution"
    assert {"position_cap", "portfolio_risk", "profile_tension"} <= {f["code"] for f in res["flags"] if f["new"]}
    # Measured correlations give a lower portfolio volatility than the priors, so the binding limit is
    # Robert's 10% single-security cap (the placeholder data stopped earlier, at 7.5%, on volatility).
    assert res["alternatives"]["max_weight_without_new_conflicts"] <= 0.10
    small = check_proposal(persona("robert"), {"type": "add", "ticker": "AGG", "weight": 0.05}, real_provider)
    assert small["status"] != "conflict"


def test_amira_crypto_cap_and_diversification(real_provider, persona):
    from alphaforge.governance import evaluate_weights
    from alphaforge.portfolio import weights_from_holdings
    from alphaforge.suggestions import build_suggestions

    amira = persona("amira")
    flags, _ = evaluate_weights(weights_from_holdings(amira["holdings"]), amira, real_provider, "3y")
    assert any(f["code"] == "crypto_cap" and f["severity"] == "conflict" for f in flags)
    s = build_suggestions(amira, real_provider)
    assert {"diversify", "crypto_cap"} <= {i["id"] for i in s["items"]}
    assert "crypto" not in [p["asset_group"] for p in s["top_picks"]]


def test_mateo_esg_screen(real_provider, persona):
    from alphaforge.radar import compute_radar
    from alphaforge.scoring import score_universe

    scored = _by_ticker(score_universe(persona("mateo"), real_provider))
    for t in ("ENB.TO", "CNQ.TO", "XOM", "BTC-USD"):
        assert scored[t]["status"] == "excluded"
    assert scored["ESGV"]["status"] != "excluded"
    radar = {a["key"]: a for a in compute_radar(persona("mateo"), real_provider)["axes"]}
    assert radar["esg"]["value"] < 100
    assert radar["income"]["value"] is not None  # dividend yields are now loaded


def test_scores_use_real_data_with_higher_confidence(real_provider, persona):
    from alphaforge.scoring import score_universe

    res = score_universe(persona("robert"), real_provider)
    assert res["window"] == "3y" and res["window_note"] is None
    nvda = {pid: _by_ticker(score_universe(persona(pid), real_provider))["NVDA"]["score"] for pid in ("amira", "robert")}
    assert nvda["amira"] > nvda["robert"] + 30
    for r in res["results"]:
        assert r["confidence"]["level"] == "high", (r["ticker"], r["confidence"])
        assert not any("placeholder" in x.lower() or "group priors" in x for x in r["confidence"]["reasons"])
