"""Behaviour the brief asks the personas to test (plan section 10)."""

from alphaforge.governance import check_proposal, evaluate_weights
from alphaforge.portfolio import weights_from_holdings
from alphaforge.radar import compute_radar
from alphaforge.scoring import score_universe
from alphaforge.suggestions import build_suggestions


def _by_ticker(result):
    return {r["ticker"]: r for r in result["results"]}


def test_robert_tsla_request_is_challenged(provider, persona):
    robert = persona("robert")
    res = check_proposal(robert, {"type": "add", "ticker": "TSLA", "weight": 0.30}, provider)
    assert res["asset_fit"]["score"] < 30
    assert res["status"] == "conflict"
    codes = {f["code"] for f in res["flags"] if f["new"]}
    assert {"position_cap", "portfolio_risk", "profile_tension"} <= codes
    assert res["alternatives"]["max_weight_without_new_conflicts"] < 0.10


def test_robert_small_defensive_addition_is_not_a_conflict(provider, persona):
    res = check_proposal(persona("robert"), {"type": "add", "ticker": "AGG", "weight": 0.05}, provider)
    assert res["status"] != "conflict"


def test_amira_crypto_capped_at_10_percent(provider, persona):
    amira = persona("amira")
    flags, _ = evaluate_weights(weights_from_holdings(amira["holdings"]), amira, provider, "5y")
    crypto = [f for f in flags if f["code"] == "crypto_cap"]
    assert crypto and crypto[0]["severity"] == "conflict"   # preset holds 20% BTC
    ok = check_proposal(amira, {"type": "allocation", "weights": {"SPY": 0.5, "QQQ": 0.4, "BTC-USD": 0.1}}, provider)
    assert not any(f["code"] == "crypto_cap" for f in ok["flags"])


def test_amira_high_risk_still_gets_diversification_suggestions(provider, persona):
    s = build_suggestions(persona("amira"), provider)
    ids = {i["id"] for i in s["items"]}
    assert "diversify" in ids and "crypto_cap" in ids
    groups = [p["asset_group"] for p in s["top_picks"]]
    assert len(set(groups)) >= 2  # top picks are not all one kind of asset
    assert "crypto" not in groups  # crypto as a group is already over her 10% cap


def test_mateo_esg_screen_excludes_fossil_fuels_but_shows_them(provider, persona):
    scored = _by_ticker(score_universe(persona("mateo"), provider))
    for t in ("ENB.TO", "CNQ.TO", "XOM"):
        assert scored[t]["status"] == "excluded"
        assert any("ESG screen" in b for b in scored[t]["badges"])
    assert scored["ESGV"]["status"] != "excluded"
    assert scored["BTC-USD"]["status"] == "excluded"  # crypto preference "avoid"


def test_mateo_radar_flags_esg_gap(provider, persona):
    radar = compute_radar(persona("mateo"), provider)
    esg = next(a for a in radar["axes"] if a["key"] == "esg")
    assert esg["target"] == 100 and esg["value"] < 100


def test_same_asset_scores_differently_across_profiles(provider, persona):
    nvda = {pid: _by_ticker(score_universe(persona(pid), provider))["NVDA"]["score"] for pid in ("amira", "robert")}
    assert nvda["amira"] > nvda["robert"] + 30


def test_every_component_carries_evidence_or_a_reason(provider, persona):
    for r in score_universe(persona("robert"), provider)["results"]:
        assert 0 <= r["score"] <= 100
        for c in r["components"]:
            assert c["explanation"]
        assert r["confidence"]["level"] == "low"  # placeholder data must lower confidence
