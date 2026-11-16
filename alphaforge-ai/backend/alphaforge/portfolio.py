"""Portfolio math shared by the radar, scoring, suggestions and governance."""

from __future__ import annotations

import math

from .data.provider import HEURISTIC_CORR_NOTE, DataProvider


def weights_from_holdings(holdings: list[dict]) -> dict[str, float]:
    total = sum(h["amount"] for h in holdings)
    if total <= 0:
        return {}
    w: dict[str, float] = {}
    for h in holdings:
        w[h["ticker"]] = w.get(h["ticker"], 0.0) + h["amount"] / total
    return w


def add_position(weights: dict[str, float], ticker: str, weight: float) -> dict[str, float]:
    """New weights after adding `weight` of `ticker`, scaling everything else down."""
    weight = min(max(weight, 0.0), 1.0)
    scaled = {t: w * (1 - weight) for t, w in weights.items()}
    scaled[ticker] = scaled.get(ticker, 0.0) + weight
    return scaled


class PortfolioStats:
    """Covariance-based statistics for one set of weights on one metrics window."""

    def __init__(self, weights: dict[str, float], provider: DataProvider, window: str):
        self.weights = {t: w for t, w in weights.items() if w > 0}
        self.provider = provider
        self.window = window
        self.heuristic_corr_used = False
        self.missing_vol: list[str] = []
        self._vols = {}
        for t in self.weights:
            v = provider.metric(t, window, "ann_vol")
            if v is None:
                self.missing_vol.append(t)
            else:
                self._vols[t] = float(v)

    def corr(self, a: str, b: str) -> float:
        c, heuristic = self.provider.correlation(a, b, self.window)
        self.heuristic_corr_used |= heuristic
        return c

    @property
    def effective_n(self) -> float:
        s = sum(w * w for w in self.weights.values())
        return 1 / s if s else 0.0

    def top_concentration(self, n: int = 3) -> float:
        return sum(sorted(self.weights.values(), reverse=True)[:n])

    def avg_pairwise_corr(self) -> float | None:
        tickers = list(self.weights)
        num = den = 0.0
        for i, a in enumerate(tickers):
            for b in tickers[i + 1:]:
                ww = self.weights[a] * self.weights[b]
                num += ww * self.corr(a, b)
                den += ww
        return num / den if den else None

    def avg_corr_with(self, ticker: str) -> float | None:
        if not self.weights:
            return None
        total = sum(self.weights.values())
        return sum(w * self.corr(ticker, t) for t, w in self.weights.items()) / total

    def _cov_row(self, a: str) -> float:
        """(Sigma w)_a over holdings with known volatility."""
        return sum(
            self.weights[b] * self._vols[a] * self._vols[b] * self.corr(a, b)
            for b in self._vols
        )

    def volatility(self) -> float | None:
        if not self._vols:
            return None
        var = sum(self.weights[a] * self._cov_row(a) for a in self._vols)
        return math.sqrt(max(var, 0.0))

    def risk_contributions(self) -> dict[str, float]:
        """Share of portfolio variance from each holding (sums to ~1)."""
        vol = self.volatility()
        if not vol:
            return {}
        var = vol * vol
        return {a: self.weights[a] * self._cov_row(a) / var for a in self._vols}

    def share(self, predicate) -> float:
        return sum(w for t, w in self.weights.items() if predicate(self.provider.assets[t]))

    def limitations(self) -> list[str]:
        notes = []
        if self.heuristic_corr_used:
            notes.append(HEURISTIC_CORR_NOTE)
        if self.missing_vol:
            notes.append(f"No volatility data for {', '.join(self.missing_vol)}; excluded from risk estimates.")
        return notes


def stress_test(weights: dict[str, float], provider: DataProvider, window: str, scenario: str) -> dict:
    """Replay a historical scenario: weighted sum of each holding's scenario return.

    Holdings without scenario data are reported, not guessed.
    """
    scenarios = provider.stress_scenarios()
    if scenario not in scenarios:
        return {"scenario": scenario, "available": False, "reason": "scenario not in loaded data"}
    covered, missing, total = 0.0, [], 0.0
    lines = []
    for t, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        r = ((provider.metrics(t, window) or {}).get("stress") or {}).get(scenario)
        if r is None:
            missing.append(t)
            continue
        covered += w
        total += w * r
        lines.append({"ticker": t, "weight": round(w, 4), "scenario_return": r, "contribution": round(w * r, 4),
                      "source_id": provider.evidence_ref(t, window, f"stress.{scenario}")})
    meta = scenarios[scenario]
    return {
        "scenario": scenario,
        "label": meta.get("label"),
        "period": f"{meta.get('start')} to {meta.get('end')}",
        "available": covered > 0,
        "portfolio_return_on_covered": round(total / covered, 4) if covered else None,
        "coverage": round(covered, 4),
        "missing": missing,
        "lines": lines,
        "method": "Weighted sum of each holding's historical return over the scenario period; "
                  "holdings without data are excluded and the result is rescaled to the covered share.",
    }
