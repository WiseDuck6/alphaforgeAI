"""Allow-listed sources for the ingest jobs, and how each asset is searched.

Only these sources are used. News articles are kept only when the publisher is on
NEWS_PUBLISHERS: wire services, national newspapers and established business media with
editorial standards and corrections policies. Aggregators, blogs, social media, press-release
wires and investment-newsletter sites are excluded on purpose.

Articles are discovered through two indexes and then filtered by publisher:
- GDELT DOC 2.0 API (open research index of world news, searchable from 2017).
- Google News RSS search (used when GDELT rate-limits the caller, which happens on shared
  IP addresses). Only the headline, publisher, date and link are stored, never article text.
"""

from __future__ import annotations

# domain -> publisher name. A subdomain matches its parent (www.reuters.com -> reuters.com).
NEWS_PUBLISHERS = {
    "reuters.com": "Reuters",
    "apnews.com": "Associated Press",
    "bloomberg.com": "Bloomberg",
    "cnbc.com": "CNBC",
    "wsj.com": "The Wall Street Journal",
    "ft.com": "Financial Times",
    "nytimes.com": "The New York Times",
    "washingtonpost.com": "The Washington Post",
    "bbc.com": "BBC", "bbc.co.uk": "BBC",
    "theguardian.com": "The Guardian",
    "economist.com": "The Economist",
    "marketwatch.com": "MarketWatch",
    "barrons.com": "Barron's",
    "theglobeandmail.com": "The Globe and Mail",
    "financialpost.com": "Financial Post",
    "cbc.ca": "CBC News",
    "bnnbloomberg.ca": "BNN Bloomberg",
    "npr.org": "NPR",
    "axios.com": "Axios",
}
# Used in search queries (GDELT and Google News limit query length, so the core set only).
QUERY_DOMAINS = ["reuters.com", "apnews.com", "bloomberg.com", "cnbc.com", "wsj.com", "ft.com", "nytimes.com",
                 "theglobeandmail.com", "financialpost.com", "cbc.ca", "bnnbloomberg.ca", "marketwatch.com", "bbc.com"]

GOOGLE_GROUPS = (
    ("reuters.com", "cnbc.com", "bloomberg.com"),
    ("theglobeandmail.com", "bnnbloomberg.ca", "financialpost.com"),
    ("wsj.com", "nytimes.com", "apnews.com"),
    ("ft.com", "marketwatch.com", "cbc.ca"),
)

OFFICIAL_FEEDS = {
    "federal_reserve": {"name": "Federal Reserve press releases (monetary policy)",
                        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "region": "US"},
    "bank_of_canada": {"name": "Bank of Canada press releases",
                       "url": "https://www.bankofcanada.ca/content_type/press-releases/feed/", "region": "CA"},
}
POLICY_PAGES = {
    "V39079": ("Bank of Canada", "https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/"),
    "DFF": ("Federal Reserve", "https://www.federalreserve.gov/monetarypolicy/openmarket.htm"),
}

# Asset ticker -> ticker used by SEC EDGAR (Canadian names cross-listed in the U.S. file 40-F / 6-K there).
SEC_TICKERS = {
    "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA", "GOOGL": "GOOGL", "AMZN": "AMZN", "TSLA": "TSLA",
    "JPM": "JPM", "JNJ": "JNJ", "PG": "PG", "XOM": "XOM",
    "RY.TO": "RY", "TD.TO": "TD", "MFC.TO": "MFC", "ENB.TO": "ENB", "CNQ.TO": "CNQ", "CNR.TO": "CNI",
    "FTS.TO": "FTS", "SHOP.TO": "SHOP",
}
NOT_SEC_FILERS = {
    "ATD.TO": "Alimentation Couche-Tard is not an SEC registrant; its filings are on SEDAR+, which has no public API.",
    "CSU.TO": "Constellation Software is not an SEC registrant; its filings are on SEDAR+, which has no public API.",
}

# Search terms per asset: query = what is sent to the news index; title = at least one of these
# words must appear in the headline, which removes articles that only mention the company.
NEWS_QUERIES = {
    "RY.TO": ('"Royal Bank of Canada" OR "RBC"', ["royal bank", "rbc"]),
    "TD.TO": ('"TD Bank" OR "Toronto-Dominion"', ["td", "toronto-dominion"]),
    "MFC.TO": ('"Manulife"', ["manulife", "john hancock"]),
    "ENB.TO": ('"Enbridge"', ["enbridge"]),
    "CNQ.TO": ('"Canadian Natural Resources"', ["canadian natural"]),
    "CNR.TO": ('"Canadian National Railway" OR "CN Rail"', ["canadian national", "cn rail"]),
    "FTS.TO": ('"Fortis Inc"', ["fortis"]),
    "ATD.TO": ('"Couche-Tard"', ["couche-tard", "circle k"]),
    "SHOP.TO": ('"Shopify"', ["shopify"]),
    "CSU.TO": ('"Constellation Software"', ["constellation software"]),
    "AAPL": ('"Apple Inc" OR "Apple shares" OR "Apple stock" OR "Tim Cook"', ["apple"]),
    "MSFT": ('"Microsoft"', ["microsoft"]),
    "NVDA": ('"Nvidia"', ["nvidia"]),
    "GOOGL": ('"Alphabet" OR "Google"', ["alphabet", "google"]),
    "AMZN": ('"Amazon.com" OR "Amazon shares" OR "Amazon stock" OR "Andy Jassy"', ["amazon"]),
    "TSLA": ('"Tesla"', ["tesla"]),
    "JPM": ('"JPMorgan"', ["jpmorgan", "jp morgan", "jamie dimon"]),
    "JNJ": ('"Johnson & Johnson"', ["johnson & johnson", "j&j"]),
    "PG": ('"Procter & Gamble"', ["procter", "p&g"]),
    "XOM": ('"Exxon"', ["exxon"]),
    "BTC-USD": ('"Bitcoin"', ["bitcoin", "crypto"]),
    "ETH-USD": ('"Ethereum" OR "Ether"', ["ethereum", "ether"]),
}

# Ingest sources shown on the Monitoring page; each maps to one EventBridge Scheduler schedule.
INGEST_SOURCES = ("market_data", "sec_edgar", "gdelt", "official_rss")


def publisher_for(domain_or_url: str) -> tuple[str | None, str | None]:
    """Return (domain, publisher) if the domain is allow-listed."""
    d = domain_or_url.lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0].split(":", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    parts = d.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in NEWS_PUBLISHERS:
            # Language editions (e.g. jp.reuters.com) are dropped: headlines are classified in English.
            if i > 0 and parts[0] not in ("www", "uk", "ca", "edition", "amp", "m"):
                return None, None
            return cand, NEWS_PUBLISHERS[cand]
    return None, None
