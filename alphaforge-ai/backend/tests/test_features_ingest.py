"""Unit tests for the indicators, the ingest parsers and the reasoning planner (no network)."""

import json
import math
import random

import pytest

from alphaforge.features import indicators as ind
from alphaforge.features.news import news_features
from alphaforge.ingest import edgar, market, news
from alphaforge.ingest.enrich import classify, is_promotional, lexicon_sentiment
from alphaforge.ingest.sources import publisher_for
from alphaforge.reasoning import classify_intent, parse_period


# ----- indicators --------------------------------------------------------------------------
def test_rsi_sma_macd_basics():
    up = [100 + i for i in range(60)]
    assert ind.rsi_wilder(up) == 100.0
    assert ind.sma(up, 20) == pytest.approx(sum(up[-20:]) / 20)
    assert ind.macd(up)["macd"] > 0
    flat = [10.0] * 60
    assert ind.atr_wilder(flat, flat, flat) == 0


def test_ols_recovers_known_betas():
    rng = random.Random(1)
    x1 = [rng.gauss(0, 1) for _ in range(300)]
    x2 = [rng.gauss(0, 1) for _ in range(300)]
    y = [0.5 + 1.5 * a - 0.7 * b + rng.gauss(0, 0.01) for a, b in zip(x1, x2)]
    res = ind.ols(y, [x1, x2])
    assert res["betas"][0] == pytest.approx(1.5, abs=0.01) and res["betas"][1] == pytest.approx(-0.7, abs=0.01)
    assert res["r2"] > 0.99


def test_garch_and_regime_model_on_simulated_returns():
    rng = random.Random(7)
    calm = [rng.gauss(0, 0.005) for _ in range(400)]
    wild = [rng.gauss(0, 0.03) for _ in range(150)]
    rets = calm + wild
    h = ind.hmm2(rets)
    assert h["distinct"] and h["p_turbulent"] > 0.9            # ends in the turbulent stretch
    assert h["vol_turbulent"] > 3 * h["vol_calm"]
    g = ind.garch11(rets)
    assert 0 < g["persistence"] < 1 and g["vol_forecast"] > 0.1   # forecast follows the recent turbulence


def test_hurst_and_tail_measures():
    rng = random.Random(3)
    noise = [rng.gauss(0, 0.01) for _ in range(1000)]
    assert 0.4 < ind.hurst_rs(noise) < 0.7                      # R/S is biased slightly above 0.5
    var, es = ind.var_es(noise)
    assert es > var > 0
    assert ind.max_drawdown([100, 120, 60, 90])[0] == pytest.approx(-0.5)


# ----- enrichment and sources ----------------------------------------------------------------
def test_classification_rules():
    assert classify("Enbridge pipeline spill prompts regulator probe")["event_type"] == "environmental_incident"
    c = classify("Apple fined 500 million euros by EU antitrust regulators")
    assert c["event_type"] == "regulatory_action" and c["materiality"] == "high" and c["sentiment"] < 0
    assert classify("Royal Bank of Canada beats estimates as profit rises")["event_type"] == "earnings"
    assert classify("Review: BMW i3 beats Tesla at the EV game")["event_type"] != "earnings"
    assert lexicon_sentiment("shares plunge after weak results") < 0 < lexicon_sentiment("shares surge to a record")
    assert is_promotional("If SpaceX and Tesla Merge, Here's What a $10,000 Investment Would Be Worth")
    assert not is_promotional("Tesla shares jump on profit beat")


def test_publisher_allow_list():
    assert publisher_for("https://www.reuters.com") == ("reuters.com", "Reuters")
    assert publisher_for("jp.reuters.com") == (None, None)       # language edition
    assert publisher_for("https://www.fool.com") == (None, None)


# ----- SEC EDGAR parsing ---------------------------------------------------------------------
def _fact(end, val, filed, form="10-K", start=None):
    r = {"end": end, "val": val, "filed": filed, "form": form, "accn": f"a-{filed}"}
    if start:
        r["start"] = start
    return r


def test_fundamentals_use_first_filed_values_and_switch_concepts():
    facts = {"us-gaap": {
        "Revenues": {"units": {"USD": [_fact("2022-12-31", 100, "2023-02-01", start="2022-01-01")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _fact("2023-12-31", 120, "2024-02-01", start="2023-01-01"),
            _fact("2023-12-31", 125, "2025-02-01", start="2023-01-01")]}},   # restated later: ignored
        "NetIncomeLoss": {"units": {"USD": [_fact("2022-12-31", 10, "2023-02-01", start="2022-01-01"),
                                            _fact("2023-12-31", 12, "2024-02-01", start="2023-01-01"),
                                            _fact("2023-09-30", 9, "2023-11-01", form="10-Q", start="2023-01-01")]}},
        "StockholdersEquity": {"units": {"USD": [_fact("2023-12-31", 50, "2024-02-01")]}},
    }, "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2099-01-15", "val": 600, "filed": "2099-02-01", "accn": "x", "form": "10-K"},
        {"end": "2099-01-15", "val": 400, "filed": "2099-02-01", "accn": "x", "form": "10-K"}]}}}}
    f = edgar.fundamentals_from_facts({"entityName": "Test", "cik": 1, "facts": facts}, "u")
    fy = f["fiscal_years"]
    assert fy["FY2023"]["revenue"] == 120 and fy["FY2022"]["revenue"] == 100
    assert fy["FY2023"]["filed"] == "2024-02-01"
    assert f["shares_outstanding"]["value"] == 1000                 # two share classes on the cover page


def test_8k_items_become_classified_filing_signals():
    rows = [{"accessionNumber": "0001-26-000001", "filingDate": "2026-10-21", "acceptanceDateTime": "2026-10-21T16:05:00.000Z",
             "form": "8-K", "items": "2.02,9.01", "primaryDocument": "x.htm", "primaryDocDescription": "8-K"},
            {"accessionNumber": "0001-26-000002", "filingDate": "2026-10-22", "acceptanceDateTime": "", "form": "8-K",
             "items": "1.05", "primaryDocument": "y.htm", "primaryDocDescription": "8-K"},
            {"accessionNumber": "0001-26-000003", "filingDate": "2026-10-23", "acceptanceDateTime": "", "form": "424B2",
             "items": "", "primaryDocument": "z.htm", "primaryDocDescription": ""}]
    sig = edgar.filing_signals("TSLA", "Consumer Discretionary", "0001318605", "Tesla", rows, "now")
    assert [s["event_type"] for s in sig] == ["earnings", "data_breach"]
    assert sig[0]["items"] == ["2.02"] and sig[0]["published_at"] == "2026-10-21T16:05:00Z"
    assert sig[1]["materiality"] == "high" and sig[1]["sentiment"] < 0 and sig[1]["esg_category"] == "social"


# ----- live market data splice and macro events -------------------------------------------------
def test_splice_rescales_live_rows_onto_the_starter_series():
    base = [{"date": "2026-11-11", "adj_close": 99.0, "close": 100.0}, {"date": "2026-11-12", "adj_close": 100.0, "close": 101.0}]
    live = {"rows": [{"date": "2026-11-12", "adj_close": 50.0, "close": 101.0, "open": 1, "high": 1, "low": 1, "volume": 5},
                     {"date": "2026-11-13", "adj_close": 55.0, "close": 111.1, "open": 1, "high": 1, "low": 1, "volume": 6}],
            "splits": []}
    pts, info = market.splice(base, live)
    assert info["anchor_date"] == "2026-11-12" and info["factor"] == 2.0
    assert pts == [{"date": "2026-11-13", "adj_close": 110.0, "open": 1, "high": 1, "low": 1, "close": 111.1, "volume": 6}]
    with pytest.raises(ValueError):
        market.splice(base, {**live, "splits": ["2026-11-13"]})


def test_policy_decisions_from_rate_series_ignore_month_end_spikes():
    series = {"series": {
        "V39079": {"label": "BoC", "points": [["2022-03-01", 0.25], ["2022-03-03", 0.5], ["2022-03-04", 0.5]]},
        "DFF": {"label": "fed funds", "points": [["2022-03-16", 0.08], ["2022-03-17", 0.33], ["2022-03-31", 0.53],
                                                  ["2022-04-01", 0.33], ["2022-04-02", 0.33]]}}}
    ev = news.macro_events(series, "now")
    titles = [e["title"] for e in ev]
    assert "Bank of Canada raised its policy rate from 0.25% to 0.50%" in titles
    assert len([e for e in ev if e["series_id"] == "DFF"]) == 1      # 0.33 -> 0.53 -> 0.33 is a spike


class _FakeHttp:
    def __init__(self, rss):
        self.rss, self.urls = rss, []

    def json(self, url, headers=None):
        from alphaforge.ingest.http import IngestHttpError
        raise IngestHttpError(url, 429, "rate limited")

    def text(self, url, headers=None):
        self.urls.append(url)
        return self.rss


def test_news_search_keeps_allow_listed_headlines_naming_the_company():
    rss = """<rss><channel>
      <item><title>Shopify shares sink after results - Reuters</title><link>https://news.google.com/a</link>
        <pubDate>Thu, 05 May 2022 12:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
      <item><title>Shopify to the moon - Some Blog</title><link>https://news.google.com/b</link>
        <pubDate>Thu, 05 May 2022 12:00:00 GMT</pubDate><source url="https://someblog.example">Some Blog</source></item>
      <item><title>Markets wrap: stocks fall - Reuters</title><link>https://news.google.com/c</link>
        <pubDate>Thu, 05 May 2022 12:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
    </channel></rss>"""
    ns = news.NewsSearch(_FakeHttp(rss))
    arts = ns.search("SHOP.TO", "2022-05-03", "2022-05-06", max_items=5)
    assert [a["title"] for a in arts] == ["Shopify shares sink after results"]
    assert arts[0]["publisher"] == "Reuters" and ns.gdelt_blocked     # GDELT refused, Google News used
    sig = news.article_signals("SHOP.TO", "IT", arts, "now", linked_move={"ticker": "SHOP.TO", "date": "2022-05-05", "return": -0.15})
    assert sig[0]["materiality"] in ("medium", "high") and sig[0]["sentiment"] < 0


def test_news_features_count_sentiment_and_controversies():
    items = [
        {"kind": "news", "published_at": "2026-11-10T00:00:00Z", "sentiment": 0.5, "materiality": "medium", "event_type": "earnings"},
        {"kind": "news", "published_at": "2026-11-01T00:00:00Z", "sentiment": -0.6, "materiality": "high",
         "event_type": "environmental_incident", "esg_category": "environmental", "title": "spill"},
        {"kind": "filing", "published_at": "2026-10-01T00:00:00Z", "sentiment": 0.0, "materiality": "low"},
        {"kind": "news", "published_at": "2026-12-01T00:00:00Z", "sentiment": 1.0},   # after the as-of date
    ]
    f, missing, det = news_features([s for s in items if s["published_at"] <= "2026-11-12T23"], "2026-11-12")
    assert f["news_count_30d"] == 2 and f["filings_90d"] == 1 and f["negative_material_90d"] == 1
    assert f["esg_controversy_level"] == "significant" and det["controversies"][0]["title"] == "spill"
    assert -1 < f["news_sentiment_30d"] < 1


# ----- reasoning planner -------------------------------------------------------------------------
@pytest.mark.parametrize("q,tickers,pct,intent", [
    ("Should I put 30% of my portfolio in Tesla?", ["TSLA"], True, "allocation"),
    ("Why did Shopify fall in 2022?", ["SHOP.TO"], False, "explain_move"),
    ("What's the latest news on my holdings?", [], False, "news"),
    ("Compare SPY and ESGV", ["SPY", "ESGV"], False, "compare"),
    ("Why is NVDA ranked low?", ["NVDA"], False, "asset"),
    ("Which assets fit me best?", [], False, "ranking"),
    ("How would my portfolio have done in 2022?", [], False, "stress"),
    ("What is the market regime right now?", [], False, "market"),
    ("How can I diversify my portfolio?", [], False, "portfolio"),
])
def test_intents(q, tickers, pct, intent):
    assert classify_intent(q, tickers, pct) == intent


def test_periods():
    assert parse_period("why did it fall in 2022", "2026-11-12") == ("2022-01-01", "2022-12-31", "2022")
    assert parse_period("what happened in october 2024", "2026-11-12")[:2] == ("2024-10-01", "2024-10-31")
    assert parse_period("news last month", "2026-11-12")[0] == "2026-10-12"
    assert parse_period("no date here", "2026-11-12") is None


def test_local_answer_has_reasoning_steps_and_resolvable_citations(provider, persona):
    import re

    from alphaforge.ask import answer_local

    a = answer_local("Should I put 30% of my portfolio in Tesla?", persona("robert"), provider)
    titles = [s["title"] for s in a["reasoning"]]
    assert titles[:3] == ["Understand the question", "Read the investor profile", "Gather market evidence"]
    assert "Run the governance check" in titles and titles[-1] == "Answer"
    assert a["governance"]["status"] == "conflict" and "conflicts with your profile" in a["answer"]
    refs = {e["ref"] for e in a["evidence"]}
    cited = {int(x) for x in re.findall(r"\[(\d+)\]", a["answer"] + json.dumps(a["reasoning"]))}
    assert cited and cited <= refs
    assert math.isclose(sum(a["asset"]["components"][i]["weight"] for i in range(9)), 1, abs_tol=1e-3)
