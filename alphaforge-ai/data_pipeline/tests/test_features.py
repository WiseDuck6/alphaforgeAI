"""Feature engine (asset_features.json) and the fit score that uses it, on the real starter data."""

import json
import shutil
import statistics
from collections import Counter
from datetime import date, timedelta

import pytest


def _features(bundle_dir):
    return json.loads((bundle_dir / "asset_features.json").read_text())


def _prices(bundle_dir, t):
    return json.loads((bundle_dir / "prices" / f"{t}.json").read_text())["points"]


def test_every_asset_has_features_with_definitions(bundle_dir):
    doc = _features(bundle_dir)
    assert len(doc["assets"]) == 31 and doc["as_of"] == "2026-11-12"
    defs = doc["definitions"]
    for t, a in doc["assets"].items():
        assert set(a["values"]) <= set(defs), (t, set(a["values"]) - set(defs))
        for k in ("ret_1y", "vol_1y", "mdd_5y", "sharpe_3y", "rsi_14", "sma_200", "atr_pct", "adv_30", "beta_1y"):
            assert a["values"].get(k) is not None, (t, k)
        assert 0 <= a["values"]["rsi_14"] <= 100
    assert set(doc["unavailable"]) >= {"forward_pe", "analyst_revisions"}


def test_technical_indicators_match_the_price_file(bundle_dir):
    pts = _prices(bundle_dir, "SPY")
    closes = [p["adj_close"] for p in pts]
    v = _features(bundle_dir)["assets"]["SPY"]["values"]
    assert v["sma_200"] == pytest.approx(statistics.fmean(closes[-200:]), rel=1e-6)
    assert v["sma_20"] == pytest.approx(statistics.fmean(closes[-20:]), rel=1e-6)
    assert v["pct_from_52w_high"] == pytest.approx(closes[-1] / max(closes[-252:]) - 1, abs=1e-4)


def test_feature_risk_numbers_agree_with_the_pipeline_metrics(bundle_dir, real_provider):
    # Two code paths (data_pipeline/build_bundle.py and alphaforge/features) on the same CAD series.
    for t in ("SPY", "XIU.TO", "TLT", "TSLA", "BTC-USD"):
        v = _features(bundle_dir)["assets"][t]["values"]
        m = real_provider.metrics(t, "5y")
        assert v["mdd_5y"] == pytest.approx(m["max_drawdown"], abs=2e-4), t
        assert v["stress_2022"] == pytest.approx(m["stress"]["2022_rates_shock"], abs=2e-4), t


def test_model_features_are_in_range(bundle_dir):
    for t, a in _features(bundle_dir)["assets"].items():
        v, det = a["values"], a["details"]
        assert 0 < v["garch_persistence"] < 1, t
        assert 0 < v["garch_vol_1m"] < 3, t
        if "p_turbulent" in v:
            assert 0 <= v["p_turbulent"] <= 1 and v["vol_turbulent_regime"] > v["vol_calm_regime"], t
        assert 0.2 < v["hurst_3y"] < 0.9, t
        assert v["es_95_1d"] >= v["var_95_1d"] > 0, t
        years = Counter(m["date"][:4] for m in det["big_moves"])
        assert all(n <= 6 for n in years.values()), (t, years)
        assert all(abs(m["z"]) >= 3 for m in det["big_moves"]), t


def test_fundamentals_and_news_are_reported_missing_without_ingest(bundle_dir):
    a = _features(bundle_dir)["assets"]
    assert "pe" not in a["AAPL"]["values"] and "ingest" in a["AAPL"]["missing"]["pe"]
    assert a["SPY"]["missing"]["pe"] == "Not applicable to a fund."
    assert a["ATD.TO"]["missing"]["roe"].startswith("Alimentation Couche-Tard is not an SEC registrant") or \
        "ingest" in a["ATD.TO"]["missing"]["roe"]
    assert a["TSLA"]["values"]["news_count_30d"] == 0 and "news_sentiment_30d" in a["TSLA"]["missing"]


def test_market_regime_has_a_known_label(bundle_dir):
    r = _features(bundle_dir)["market_regime"]
    assert r["label"] in {"stress", "correction", "volatile", "calm_uptrend", "mixed"}
    assert r["rates"]["ca_policy_rate"]["series_id"] == "V39079" and r["rules"]


# ----- fit score on real features ------------------------------------------------------
@pytest.fixture
def persona(real_provider):
    from alphaforge.profiles import preset_profile

    return lambda pid: preset_profile(pid, set(real_provider.assets))


def _by(res):
    return {r["ticker"]: r for r in res["results"]}


def test_robert_tsla_is_outside_profile_whatever_the_weights(real_provider, persona):
    from alphaforge.scoring import score_universe

    res = score_universe(persona("robert"), real_provider)
    tsla = _by(res)["TSLA"]
    assert tsla["verdict"]["key"] == "outside_profile" and tsla["score"] <= 45
    assert tsla["rank_stability"]["p_bottom5"] >= 0.9   # robust to +/-25% weight changes
    assert any("capped at 45" in g for g in tsla["gates"]) or tsla["raw_score"] <= 45
    assert res["investor"]["drawdown_tolerance"] == 0.20
    assert abs(sum(res["component_weights"].values()) - 1) < 1e-3


def test_components_that_do_not_apply_share_their_weight(real_provider, persona):
    from alphaforge.scoring import score_universe

    spy = _by(score_universe(persona("mateo"), real_provider))["SPY"]
    comp = {c["key"]: c for c in spy["components"]}
    assert comp["fundamentals"]["applicable"] is False and comp["fundamentals"]["weight"] == 0
    assert sum(c["weight"] for c in spy["components"]) == pytest.approx(1, abs=1e-3)


def test_long_horizon_low_risk_profile_keeps_a_tight_loss_tolerance(real_provider, persona):
    from alphaforge.profile_features import investor_features

    p = dict(persona("robert"), horizon_years=20)
    inv = investor_features(p, real_provider)
    assert inv["drawdown_tolerance"] == 0.20 and "risk level" in inv["drawdown_tolerance_source"]
    assert inv["horizon_beyond_data"] is True


def test_esg_controversy_in_news_lowers_fit_only_for_esg_profiles(bundle_dir, tmp_path, persona):
    from alphaforge.data.provider import load_from_dir
    from alphaforge.features import build_features, dir_reader
    from alphaforge.scoring import score_universe

    shutil.copytree(bundle_dir, tmp_path / "b")
    b = tmp_path / "b"
    when = (date(2026, 11, 12) - timedelta(days=20)).isoformat() + "T12:00:00Z"
    sig = {"signal_id": "t1", "kind": "news", "source_id": "google_news", "source_tier": "major_media",
           "publisher": "Reuters", "domain": "reuters.com", "published_at": when, "title": "CN Rail derailment spill prompts probe",
           "source_url": "https://www.reuters.com/x", "tickers": ["CNR.TO"], "event_type": "environmental_incident",
           "esg_category": "environmental", "materiality": "high", "sentiment": -0.6, "severity": 5}
    (b / "signals.json").write_text(json.dumps({"signals": [sig]}))
    (b / "asset_features.json").write_text(json.dumps(build_features(dir_reader(b))))
    base = load_from_dir(bundle_dir)
    hit = load_from_dir(b)
    assert hit.feature("CNR.TO", "esg_controversy_level") == "significant"

    def pref(provider, pid):
        r = _by(score_universe(persona(pid), provider))["CNR.TO"]
        return next(c for c in r["components"] if c["key"] == "preference_fit")

    assert pref(base, "mateo")["score"] - pref(hit, "mateo")["score"] == pytest.approx(30, abs=0.1)
    assert "controversy" in pref(hit, "mateo")["explanation"]
    assert pref(base, "robert")["score"] == pref(hit, "robert")["score"]   # no ESG preference


# ----- Ask AlphaForge on real data -------------------------------------------------------
def test_ask_explains_a_past_move_with_prices_and_policy_context(real_provider, persona):
    from alphaforge.ask import answer_local

    a = answer_local("Why did Shopify fall in 2022?", persona("robert"), real_provider)
    assert a["intent"] == "explain_move"
    assert "2022" in a["answer"] and "Bank of Canada raised rates 7 times" in a["answer"]
    titles = [s["title"] for s in a["reasoning"]]
    assert titles[0] == "Understand the question" and "Find the moves in the price data" in titles
    refs = {e["ref"] for e in a["evidence"]}
    import re
    assert {int(x) for x in re.findall(r"\[(\d+)\]", a["answer"])} <= refs   # every citation resolves


def test_ask_ranking_lists_eligible_assets_only(real_provider, persona):
    from alphaforge.ask import answer_local

    a = answer_local("Which assets fit me best?", persona("mateo"), real_provider)
    assert a["intent"] == "ranking"
    tops = [r["ticker"] for r in a["ranking"]]
    assert len(tops) == 5 and not {"XOM", "ENB.TO", "CNQ.TO", "BTC-USD", "ETH-USD"} & set(tops)
