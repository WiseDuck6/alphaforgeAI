"""News, official statements and dated macro events, filtered to allow-listed sources.

- search(): articles about one asset in a date window, from GDELT, else Google News RSS.
  Both are discovery indexes; an article is kept only if its publisher is allow-listed
  and the headline names the company.
- official_feeds(): Federal Reserve and Bank of Canada press releases (RSS).
- macro_events(): policy-rate decisions read from the macro series in the bundle
  (Bank of Canada target rate changes, fed funds moves of 0.15 points or more).
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .enrich import classify, is_promotional, severity, signal_id
from .http import Http, IngestHttpError
from .sources import GOOGLE_GROUPS, NEWS_QUERIES, OFFICIAL_FEEDS, POLICY_PAGES, QUERY_DOMAINS, publisher_for


class NewsSearch:
    """Keeps state across calls: once GDELT rate-limits us, the run continues on Google News."""

    def __init__(self, http: Http, use_gdelt: bool = True, use_google: bool = True):
        self.http = http
        self.use_gdelt, self.use_google = use_gdelt, use_google
        self.gdelt_blocked = False
        self.stats = {"gdelt_queries": 0, "google_queries": 0, "gdelt_errors": 0, "google_errors": 0,
                      "kept": 0, "dropped_publisher": 0, "dropped_title": 0, "dropped_promotional": 0}

    def search(self, ticker: str, start: str, end: str, max_items: int = 15) -> list[dict]:
        query, terms = NEWS_QUERIES[ticker]
        arts: list[dict] = []
        if self.use_gdelt and not self.gdelt_blocked:
            try:
                arts = self._gdelt(query, start, end, max_items * 3)
                self.stats["gdelt_queries"] += 1
            except IngestHttpError:
                self.stats["gdelt_errors"] += 1
                self.gdelt_blocked = True
        if not arts and self.use_google:
            arts = self._google(query, start, end, max_items)
        rx = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
        out, seen = [], set()
        for a in arts:
            domain, publisher = publisher_for(a["publisher_url"])
            if not domain:
                self.stats["dropped_publisher"] += 1
                continue
            if not rx.search(a["title"]):
                self.stats["dropped_title"] += 1
                continue
            if is_promotional(a["title"]):
                self.stats["dropped_promotional"] += 1
                continue
            key = re.sub(r"\W+", " ", a["title"].lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            out.append({**a, "domain": domain, "publisher": publisher})
            if len(out) >= max_items:
                break
        self.stats["kept"] += len(out)
        return out

    def _gdelt(self, query: str, start: str, end: str, n: int) -> list[dict]:
        domains = " OR ".join(f"domain:{d}" for d in QUERY_DOMAINS)
        q = f"({query}) sourcelang:english ({domains})"
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({
            "query": q, "mode": "artlist", "format": "json", "maxrecords": min(n, 250), "sort": "datedesc",
            "startdatetime": start.replace("-", "") + "000000", "enddatetime": end.replace("-", "") + "235959"}))
        doc = self.http.json(url)
        out = []
        for a in doc.get("articles", []):
            ts = a.get("seendate", "")
            try:
                published = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            out.append({"title": a.get("title", "").strip(), "url": a.get("url"), "publisher_url": a.get("domain", ""),
                        "published_at": published.isoformat().replace("+00:00", "Z"), "via": "gdelt"})
        return out

    def _google(self, query: str, start: str, end: str, max_items: int) -> list[dict]:
        """Google News returns far fewer results for long site: lists, so the allow-list is
        queried in small groups until enough headlines are found."""
        out: list[dict] = []
        seen = set()
        for group in GOOGLE_GROUPS:
            try:
                got = self._google_group(query, group, start, end)
                self.stats["google_queries"] += 1
            except (IngestHttpError, ET.ParseError):
                self.stats["google_errors"] += 1
                continue
            for a in got:
                if a["title"] not in seen:
                    seen.add(a["title"])
                    out.append(a)
            if len(out) >= max_items:
                break
        return out

    def _google_group(self, query: str, group: tuple[str, ...], start: str, end: str) -> list[dict]:
        sites = " OR ".join(f"site:{d}" for d in group)
        before = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        q = f"{query} ({sites}) after:{start} before:{before}"
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-CA", "gl": "CA", "ceid": "CA:en"})
        root = ET.fromstring(self.http.text(url))
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            src = item.find("source")
            pub_url = src.get("url") if src is not None else ""
            pub_name = (src.text or "").strip() if src is not None else ""
            if pub_name and title.endswith(" - " + pub_name):
                title = title[: -len(pub_name) - 3].strip()
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            d = published.date().isoformat()
            if not start <= d <= end:
                continue
            out.append({"title": title, "url": item.findtext("link"), "publisher_url": pub_url or "",
                        "published_at": published.isoformat().replace("+00:00", "Z"), "via": "google_news"})
        return out


def article_signals(ticker: str, sector: str, articles: list[dict], retrieved_at: str, linked_move: dict | None = None) -> list[dict]:
    out = []
    for a in articles:
        c = classify(a["title"])
        mat = c["materiality"]
        if linked_move and mat == "low":
            mat = "medium"  # published next to an unusual price move
        out.append({
            "signal_id": signal_id("news", a["url"] or a["title"]), "kind": "news",
            "source_id": "gdelt" if a["via"] == "gdelt" else "google_news", "source_tier": "major_media",
            "publisher": a["publisher"], "domain": a["domain"], "published_at": a["published_at"],
            "retrieved_at": retrieved_at, "title": a["title"], "summary": "", "source_url": a["url"],
            "discovered_via": a["via"], "tickers": [ticker], "sectors": [sector],
            "event_type": c["event_type"], "esg_category": c["esg_category"], "materiality": mat,
            "sentiment": c["sentiment"], "sentiment_method": c["sentiment_method"],
            "severity": severity(mat, c["sentiment"]), "classification_rule": c["classification_rule"],
            **({"linked_move": linked_move} if linked_move else {}),
        })
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def official_feeds(http: Http, retrieved_at: str) -> tuple[list[dict], list[str]]:
    out, errors = [], []
    for fid, feed in OFFICIAL_FEEDS.items():
        try:
            root = ET.fromstring(http.text(feed["url"]))
        except (IngestHttpError, ET.ParseError) as e:
            errors.append(f"{fid}: {e}")
            continue
        for item in (el for el in root.iter() if _local(el.tag) == "item"):
            fields = {_local(c.tag): (c.text or "").strip() for c in item}
            title, link = fields.get("title", ""), fields.get("link") or item.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
            raw = fields.get("pubDate") or fields.get("date") or ""
            try:
                ts = parsedate_to_datetime(raw) if "," in raw else datetime.fromisoformat(raw.replace("Z", "+00:00"))
                ts = ts.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            c = classify(title)
            out.append({
                "signal_id": signal_id("official", link or title), "kind": "official", "source_id": "official_rss",
                "source_tier": "official", "publisher": feed["name"].split(" press")[0], "domain": urllib.parse.urlsplit(link or "").netloc,
                "published_at": ts.isoformat().replace("+00:00", "Z"), "retrieved_at": retrieved_at, "title": title,
                "summary": re.sub(r"<[^>]+>", "", fields.get("description", ""))[:300], "source_url": link,
                "tickers": [], "sectors": ["macro"], "region": feed["region"], "event_type": "monetary_policy"
                if c["event_type"] in ("general", "monetary_policy") else c["event_type"], "esg_category": None,
                "materiality": "medium", "sentiment": 0.0, "sentiment_method": "not_scored_official",
                "severity": 3, "classification_rule": c["classification_rule"],
            })
    return out, errors


def macro_events(macro_series: dict, retrieved_at: str, live_macro: dict | None = None) -> list[dict]:
    out = []
    for sid, threshold in (("V39079", 0.001), ("DFF", 0.15)):
        block = (macro_series.get("series") or {}).get(sid) or {}
        pts = dict(block.get("points") or [])
        pts.update(dict((live_macro or {}).get(sid) or []))
        series = sorted(pts.items())
        who, page = POLICY_PAGES[sid]
        prev = None
        for d, v in series:
            if prev is not None and abs(v - prev[1]) >= threshold:
                # For the fed funds rate, ignore one-day month-end spikes that revert the next day.
                direction = "raised" if v > prev[1] else "cut"
                title = (f"Bank of Canada {direction} its policy rate from {prev[1]:.2f}% to {v:.2f}%" if sid == "V39079"
                         else f"U.S. effective fed funds rate moved from {prev[1]:.2f}% to {v:.2f}% (FOMC decision)")
                out.append({
                    "signal_id": signal_id("macro", sid, d), "kind": "macro", "source_id": "dataset",
                    "source_tier": "official", "publisher": who, "domain": urllib.parse.urlsplit(page).netloc,
                    "published_at": f"{d}T00:00:00Z", "retrieved_at": retrieved_at, "title": title,
                    "summary": f"Read from series {sid} ({block.get('label') or sid}); decision history on the {who} site.",
                    "source_url": page, "tickers": [], "sectors": ["macro"], "region": "CA" if sid == "V39079" else "US",
                    "series_id": sid, "value_before": prev[1], "value_after": v, "event_type": "monetary_policy",
                    "esg_category": None, "materiality": "high", "sentiment": 0.0, "sentiment_method": "not_scored_macro",
                    "severity": 4, "classification_rule": f"change in {sid}",
                })
            prev = (d, v)
    # Drop fed funds "moves" that reverse within three days (month-end technical spikes): both the
    # spike and its reversal are left out.
    dff = [s for s in out if s["series_id"] == "DFF"]
    skip = set()
    for a, b in zip(dff, dff[1:]):
        days = (date.fromisoformat(b["published_at"][:10]) - date.fromisoformat(a["published_at"][:10])).days
        if days <= 3 and abs(b["value_after"] - a["value_before"]) < 0.05:
            skip.update({a["signal_id"], b["signal_id"]})
    return [s for s in out if s["signal_id"] not in skip]
