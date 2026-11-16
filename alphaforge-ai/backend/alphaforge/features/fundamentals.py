"""Company fundamentals ratios from fundamentals.json (SEC EDGAR XBRL company facts).

fundamentals.json is written by the ingest job (alphaforge.ingest.edgar). Each issuer has
annual values per fiscal year, with the date each annual report was filed, so a ratio only
uses figures that were public on the as-of date (point in time).

Banks and insurers: EV/EBITDA, net debt/EBITDA, ROIC, debt/equity and FCF yield are not
meaningful (deposits and policy liabilities are not financing debt), so they are left out
with a reason instead of being computed.
"""

from __future__ import annotations

FINANCIAL_SECTORS = {"Financials"}
NOT_MEANINGFUL_FOR_FINANCIALS = ("ev_ebitda", "net_debt_ebitda", "roic", "debt_to_equity", "fcf_yield", "fcf")


def _years_available(issuer: dict, as_of: str) -> list[tuple[str, dict]]:
    years = [(fy, v) for fy, v in (issuer.get("fiscal_years") or {}).items() if (v.get("filed") or "9999") <= as_of]
    return sorted(years, key=lambda kv: kv[1].get("end") or kv[0])


def _div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def _growth(cur, prev):
    if cur is None or prev in (None, 0) or prev < 0:
        return None
    return cur / prev - 1


def ratios(issuer: dict | None, market_cap_cad: float | None, to_cad: float, sector: str, as_of: str) -> tuple[dict, dict, dict]:
    """Return (features, missing_reasons, provenance) for one issuer."""
    out: dict = {}
    missing: dict = {}
    if not issuer:
        return out, missing, {}
    years = _years_available(issuer, as_of)
    if not years:
        missing["fundamentals"] = issuer.get("note") or \
            "No annual report filed on or before the as-of date in SEC EDGAR XBRL data."
        return out, missing, {}
    fy, cur = years[-1]
    prev = years[-2][1] if len(years) >= 2 else {}
    k = to_cad  # fundamentals are converted to CAD so they compare with the CAD market cap

    def g(d, key):
        v = d.get(key)
        return None if v is None else v * k

    revenue, ni, equity, assets = g(cur, "revenue"), g(cur, "net_income"), g(cur, "equity"), g(cur, "assets")
    op, da, cfo, capex = g(cur, "operating_income"), g(cur, "da"), g(cur, "cfo"), g(cur, "capex")
    debt, cash, divs = g(cur, "debt"), g(cur, "cash"), g(cur, "dividends_paid")
    tax, pretax = cur.get("income_tax"), cur.get("pretax_income")

    out["revenue_growth"] = _growth(revenue, g(prev, "revenue"))
    out["earnings_growth"] = _growth(ni, g(prev, "net_income"))
    prev_equity = g(prev, "equity")
    avg_equity = (equity + prev_equity) / 2 if equity is not None and prev_equity is not None else equity
    if divs is not None:
        divs = abs(divs)
    if capex is not None:
        capex = abs(capex)
    out["roe"] = _div(ni, avg_equity) if avg_equity and avg_equity > 0 else None
    out["roa"] = _div(ni, assets)
    ebitda = None if op is None or da is None else op + da
    tax_rate = 0.25
    if tax is not None and pretax not in (None, 0) and pretax > 0:
        tax_rate = min(max(tax / pretax, 0.0), 0.40)
    invested = None if equity is None else equity + (debt or 0) - (cash or 0)
    out["roic"] = _div(None if op is None else op * (1 - tax_rate), invested) if invested and invested > 0 else None
    out["debt_to_equity"] = _div(debt, equity) if equity and equity > 0 else None
    out["net_debt_ebitda"] = _div(None if debt is None else debt - (cash or 0), ebitda) if ebitda and ebitda > 0 else None
    fcf = None if cfo is None or capex is None else cfo - capex
    out["fcf"] = fcf
    out["payout_ratio"] = _div(divs, ni) if ni and ni > 0 and divs is not None else None
    if market_cap_cad:
        out["pe"] = _div(market_cap_cad, ni) if ni and ni > 0 else None
        out["pb"] = _div(market_cap_cad, equity) if equity and equity > 0 else None
        ev = market_cap_cad + (debt or 0) - (cash or 0)
        out["ev_ebitda"] = _div(ev, ebitda) if ebitda and ebitda > 0 else None
        out["fcf_yield"] = _div(fcf, market_cap_cad)
        if ni is not None and ni <= 0:
            missing["pe"] = "Net income was not positive in the latest fiscal year, so P/E is not meaningful."
    else:
        for key in ("pe", "pb", "ev_ebitda", "fcf_yield"):
            missing[key] = "No market capitalisation (shares outstanding not found in filings)."

    if sector in FINANCIAL_SECTORS:
        for key in NOT_MEANINGFUL_FOR_FINANCIALS:
            out.pop(key, None)
            missing[key] = "Not meaningful for banks and insurers (deposits and policy liabilities are not financing debt)."
    for key, v in list(out.items()):
        if v is None:
            out.pop(key)
            missing.setdefault(key, "An input was not reported in the XBRL data for the latest fiscal year.")
    prov = {"fiscal_year": fy, "period_end": cur.get("end"), "filed": cur.get("filed"), "form": cur.get("form"),
            "taxonomy": issuer.get("taxonomy"), "reporting_currency": issuer.get("currency"),
            "prior_fiscal_year": years[-2][0] if len(years) >= 2 else None,
            "source_url": issuer.get("source_url")}
    return out, missing, prov
