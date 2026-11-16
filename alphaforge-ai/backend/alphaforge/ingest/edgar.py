"""SEC EDGAR: filings (8-K items, periodic reports), XBRL fundamentals and Form 4 insider trades.

Endpoints (public, no key; a descriptive User-Agent is required):
  https://www.sec.gov/files/company_tickers.json          ticker -> CIK
  https://data.sec.gov/submissions/CIK##########.json      filing history
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   XBRL facts
  https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>  filing documents (Form 4 XML)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from .enrich import EIGHT_K_ITEMS, MATERIALITY_RANK, PERIODIC_FORMS, severity, signal_id
from .http import Http, IngestHttpError
from .sources import NOT_SEC_FILERS, SEC_TICKERS

# Annual values come from annual reports. Some Canadian issuers (e.g. CN Rail) tag their full-year
# statements in a 6-K instead of the 40-F, so 6-K facts are accepted when they cover a full year
# (flows are checked to span 330-380 days; balance-sheet values are only used at fiscal year ends).
ANNUAL_FORMS = {"10-K", "10-K/A", "40-F", "40-F/A", "20-F", "20-F/A", "6-K", "6-K/A"}
CONCEPTS = {
    "us-gaap": {
        "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"],
        "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
        "operating_income": ["OperatingIncomeLoss"],
        "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                          "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
        "income_tax": ["IncomeTaxExpenseBenefit"],
        "da": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet",
               "DepreciationAmortizationAndOther", "Depreciation"],
        "cfo": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
        "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
        "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
        "assets": ["Assets"],
        "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
        "debt_total": ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"],
        "debt_noncurrent": ["LongTermDebtNoncurrent"],
        "debt_current": ["LongTermDebtCurrent", "DebtCurrent"],
        "short_borrowings": ["ShortTermBorrowings", "CommercialPaper"],
    },
    "ifrs-full": {
        "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
        "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
        "operating_income": ["ProfitLossFromOperatingActivities"],
        "pretax_income": ["ProfitLossBeforeTax"],
        "income_tax": ["IncomeTaxExpenseContinuingOperations"],
        "da": ["DepreciationAndAmortisationExpense", "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"],
        "cfo": ["CashFlowsFromUsedInOperatingActivities"],
        "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfPropertyPlantAndEquipment"],
        "dividends_paid": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities",
                           "DividendsPaid", "DividendsRecognisedAsDistributionsToOwnersOfParent"],
        "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
        "assets": ["Assets"],
        "cash": ["CashAndCashEquivalents"],
        "debt_total": ["Borrowings"],
        "debt_noncurrent": ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings"],
        "debt_current": ["CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings", "CurrentPortionOfLongtermBorrowings"],
        "short_borrowings": ["ShorttermBorrowings"],
    },
}
FLOW_KEYS = {"revenue", "net_income", "operating_income", "pretax_income", "income_tax", "da", "cfo", "capex", "dividends_paid"}


def cik_map(http: Http) -> dict[str, str]:
    data = http.json("https://www.sec.gov/files/company_tickers.json")
    by_sec = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    return {t: by_sec[s] for t, s in SEC_TICKERS.items() if s in by_sec}


def filings(http: Http, cik: str, since: str) -> tuple[dict, list[dict]]:
    """Filing history since a date (follows the older-filings pages for frequent filers)."""
    doc = http.json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    out = _rows(doc["filings"]["recent"])
    for f in doc["filings"].get("files", []):
        if f.get("filingTo", "9999") < since:
            continue
        try:
            out += _rows(http.json(f"https://data.sec.gov/submissions/{f['name']}"))
        except IngestHttpError:
            continue
    meta = {"name": doc.get("name"), "cik": cik, "tickers": doc.get("tickers"), "fiscal_year_end": doc.get("fiscalYearEnd")}
    return meta, [r for r in out if r["filingDate"] >= since]


def _rows(block: dict) -> list[dict]:
    keys = ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument", "primaryDocDescription")
    n = len(block.get("accessionNumber", []))
    return [{k: (block.get(k) or [""] * n)[i] for k in keys} for i in range(n)]


def _doc_url(cik: str, r: dict) -> str:
    acc = r["accessionNumber"].replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{r['primaryDocument']}"


def _index_url(cik: str, r: dict) -> str:
    acc = r["accessionNumber"].replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{r['accessionNumber']}-index.htm"


def filing_signals(ticker: str, sector: str, cik: str, company: str, rows: list[dict], retrieved_at: str) -> list[dict]:
    out = []
    for r in rows:
        form = r["form"]
        published = (r.get("acceptanceDateTime") or f"{r['filingDate']}T00:00:00.000Z")
        published = published[:19] + "Z" if published[10:11] == "T" else f"{r['filingDate']}T00:00:00Z"
        if form in ("8-K", "8-K/A"):
            items = [i.strip() for i in (r.get("items") or "").split(",") if i.strip() and i.strip() != "9.01"]
            known = [i for i in items if i in EIGHT_K_ITEMS]
            if not known:
                continue
            top = max(known, key=lambda i: MATERIALITY_RANK[EIGHT_K_ITEMS[i][2]])
            label, event, mat, esg, negative = EIGHT_K_ITEMS[top]
            sentiment = -0.5 if negative else 0.0
            title = f"{company}: Form {form}, " + "; ".join(f"Item {i} {EIGHT_K_ITEMS[i][0].lower()}" for i in known)
            out.append({
                "signal_id": signal_id("sec", r["accessionNumber"]), "kind": "filing", "source_id": "sec_edgar",
                "source_tier": "official", "publisher": "SEC EDGAR", "domain": "sec.gov",
                "published_at": published, "retrieved_at": retrieved_at, "title": title,
                "summary": f"{label}. Filed {r['filingDate']}.", "source_url": _index_url(cik, r),
                "tickers": [ticker], "sectors": [sector], "form": form, "items": known,
                "event_type": event, "esg_category": esg, "materiality": mat, "sentiment": sentiment,
                "sentiment_method": "filing_type", "severity": severity(mat, sentiment),
                "classification_rule": f"8-K item {top}",
            })
        elif form in PERIODIC_FORMS:
            out.append({
                "signal_id": signal_id("sec", r["accessionNumber"]), "kind": "filing", "source_id": "sec_edgar",
                "source_tier": "official", "publisher": "SEC EDGAR", "domain": "sec.gov",
                "published_at": published, "retrieved_at": retrieved_at,
                "title": f"{company}: {PERIODIC_FORMS[form]} (Form {form})", "summary": f"Filed {r['filingDate']}.",
                "source_url": _index_url(cik, r), "tickers": [ticker], "sectors": [sector], "form": form, "items": [],
                "event_type": "periodic_report", "esg_category": None, "materiality": "low", "sentiment": 0.0,
                "sentiment_method": "filing_type", "severity": 2, "classification_rule": f"form {form}",
            })
    return out


# ----- XBRL fundamentals -------------------------------------------------------
def _annual_facts(facts: dict, concepts: list[str], unit: str, flow: bool) -> dict[str, dict]:
    """end date -> first-filed annual value (point in time).

    Concepts are tried in priority order for each period end, because companies switch
    concepts over time (Apple reported "Revenues" before 2018 and
    "RevenueFromContractWithCustomerExcludingAssessedTax" after).
    """
    merged: dict[str, dict] = {}
    for c in concepts:
        rows = ((facts.get(c) or {}).get("units") or {}).get(unit) or []
        best: dict[str, dict] = {}
        for r in rows:
            if r.get("form") not in ANNUAL_FORMS:
                continue
            if flow:
                if not r.get("start"):
                    continue
                days = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
                if not 330 <= days <= 380:
                    continue
            cur = best.get(r["end"])
            if cur is None or r["filed"] < cur["filed"]:
                best[r["end"]] = {"val": r["val"], "filed": r["filed"], "form": r["form"], "concept": c}
        for end, v in best.items():
            merged.setdefault(end, v)
    return merged


def fundamentals_from_facts(doc: dict, source_url: str) -> dict | None:
    facts_all = doc.get("facts") or {}
    tax = "us-gaap" if "us-gaap" in facts_all else "ifrs-full" if "ifrs-full" in facts_all else None
    if not tax:
        return None
    facts = facts_all[tax]
    counts: dict[str, int] = {}
    for c in CONCEPTS[tax]["revenue"] + CONCEPTS[tax]["net_income"] + CONCEPTS[tax]["assets"]:
        for u, rows in ((facts.get(c) or {}).get("units") or {}).items():
            counts[u] = counts.get(u, 0) + len(rows)
    unit = max((u for u in counts if u in ("USD", "CAD")), key=lambda u: counts[u], default=None)
    if not unit:
        return None
    series = {k: _annual_facts(facts, cs, unit, k in FLOW_KEYS) for k, cs in CONCEPTS[tax].items()}
    ends = sorted(set(series["net_income"]) | set(series["revenue"]))
    years = {}
    for end in ends[-4:]:
        row = {"end": end}
        filed = []
        for k in ("revenue", "net_income", "operating_income", "pretax_income", "income_tax", "da", "cfo", "capex",
                  "dividends_paid", "equity", "assets", "cash"):
            v = series[k].get(end)
            if v:
                row[k] = v["val"]
                filed.append((v["filed"], v["form"]))
        total = series["debt_total"].get(end)
        if total:
            debt = total["val"]
        else:
            nc, cur = series["debt_noncurrent"].get(end), series["debt_current"].get(end)
            debt = (nc["val"] if nc else 0) + (cur["val"] if cur else 0) if (nc or cur) else None
        sb = series["short_borrowings"].get(end)
        if debt is not None or sb:
            row["debt"] = (debt or 0) + (sb["val"] if sb else 0)
        if not filed:
            continue
        row["filed"], row["form"] = min(filed)
        years[f"FY{end[:4]}"] = row
    shares = _shares(facts_all, tax)
    return {"entity": doc.get("entityName"), "cik": str(doc.get("cik")).zfill(10), "taxonomy": tax, "currency": unit,
            "fiscal_years": years, "shares_outstanding": shares, "source_url": source_url,
            "concepts": {k: sorted({v["concept"] for v in s.values()}) for k, s in series.items() if s}}


SHARE_CONCEPTS = [
    ("dei", "EntityCommonStockSharesOutstanding", "cover page shares outstanding"),
    ("us-gaap", "CommonStockSharesOutstanding", "balance-sheet shares outstanding"),
    ("ifrs-full", "NumberOfSharesOutstanding", "shares outstanding (IFRS)"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "weighted average diluted shares (latest period)"),
    ("ifrs-full", "AdjustedWeightedAverageShares", "weighted average diluted shares (IFRS, latest period)"),
]


def _shares(facts_all: dict, tax: str) -> dict | None:
    """Latest share count. Multi-class issuers (e.g. Alphabet) report cover-page shares by class,
    which company facts omit, so balance-sheet and weighted-average counts are the fallbacks."""
    for taxonomy, concept, label in SHARE_CONCEPTS:
        rows = [r for r in ((facts_all.get(taxonomy) or {}).get(concept) or {}).get("units", {}).get("shares", [])
                if r.get("val") and r["val"] > 0]
        if not rows:
            continue
        latest_end = max(r["end"] for r in rows)
        if latest_end < (date.today() - timedelta(days=730)).isoformat():
            continue  # stale (e.g. a concept the issuer stopped using years ago)
        same = [r for r in rows if r["end"] == latest_end]
        latest_accn = max(same, key=lambda r: r["filed"])["accn"]
        vals = [r for r in same if r["accn"] == latest_accn]
        # Several values for one filing and date are share classes on the cover page; elsewhere keep one.
        total = sum(r["val"] for r in vals) if concept == "EntityCommonStockSharesOutstanding" else vals[0]["val"]
        return {"value": total, "as_of": latest_end, "filed": max(r["filed"] for r in same), "concept": f"{taxonomy}:{concept}",
                "method": label}
    return None


# ----- Form 4 insider transactions ---------------------------------------------------
def insider_summary(http: Http, cik: str, rows: list[dict], as_of: str, max_filings: int = 25) -> dict:
    start = (date.fromisoformat(as_of) - timedelta(days=90)).isoformat()
    form4 = [r for r in rows if r["form"] == "4" and start <= r["filingDate"] <= as_of][:max_filings]
    buys = sells = 0
    net_shares = net_value = 0.0
    parsed = 0
    for r in form4:
        doc = r["primaryDocument"].split("/")[-1]
        acc = r["accessionNumber"].replace("-", "")
        try:
            xml = http.text(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}")
            root = ET.fromstring(xml)
        except (IngestHttpError, ET.ParseError):
            continue
        parsed += 1
        for tx in root.iter("nonDerivativeTransaction"):
            code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
            if code not in ("P", "S"):
                continue
            try:
                shares = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
                price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
            except ValueError:
                continue
            sign = 1 if code == "P" else -1
            buys += code == "P"
            sells += code == "S"
            net_shares += sign * shares
            net_value += sign * shares * price
    return {"insider_buys_90d": buys, "insider_sells_90d": sells, "insider_net_shares_90d": round(net_shares),
            "insider_net_value_90d": round(net_value), "form4_filings_90d": len(form4), "form4_parsed": parsed,
            "window": f"{start} to {as_of}",
            "method": "Open-market purchases (P) and sales (S) in the non-derivative table of each Form 4; "
                      "grants, option exercises and tax withholding are excluded."}


def run(http: Http, assets: dict[str, dict], since: str, as_of: str, insider: bool = True) -> dict:
    """Fetch filings, fundamentals and insider data for every SEC filer in the universe."""
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors, signals, issuers, insiders = [], [], {}, {}
    try:
        ciks = cik_map(http)
    except IngestHttpError as e:
        return {"signals": [], "issuers": {}, "insider": {}, "errors": [str(e)], "not_covered": NOT_SEC_FILERS}
    for t, cik in ciks.items():
        a = assets.get(t)
        if not a:
            continue
        try:
            meta, rows = filings(http, cik, since)
            signals += filing_signals(t, a["sector"], cik, a["name"], rows, retrieved)
            if insider and not t.endswith(".TO"):
                insiders[t] = insider_summary(http, cik, rows, as_of)
        except (IngestHttpError, KeyError) as e:
            errors.append(f"{t} filings: {e}")
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            f = fundamentals_from_facts(http.json(url), url)
            if f:
                issuers[t] = f
                if not f["fiscal_years"]:
                    f["note"] = ("SEC company facts for this issuer contain no full-year values (only quarterly facts), "
                                 "so annual ratios are not computed.")
            else:
                errors.append(f"{t}: no usable XBRL facts")
        except IngestHttpError as e:
            errors.append(f"{t} company facts: {e}")
    missing = {t: "No CIK found in SEC company_tickers.json." for t in SEC_TICKERS if t not in ciks}
    return {"signals": signals, "issuers": issuers, "insider": insiders, "errors": errors,
            "not_covered": {**NOT_SEC_FILERS, **missing}}
