"""Curated-data access layer.

The scoring, radar and suggestion engines never read raw CSVs. They read a
*curated bundle* of JSON files whose schema is documented in
docs/data_contract.md:

    assets.json             required  asset metadata (tags, region, ESG flags)
    asset_metrics.json      required  per-window risk/return metrics
    correlations.json       optional  pairwise correlation matrix per window
    signals.json            optional  enriched events/posts/filings
    macro_snapshot.json     optional  latest macro values with availability dates
    data_quality_report.json optional
    asset_features.json     optional  feature engine output (returns, risk, technicals, fundamentals, news)
    fundamentals.json       optional  SEC EDGAR XBRL fundamentals and Form 4 insider data (ingest)
    live_extension.json     optional  prices and rates after the starter window (ingest)
    ingest_status.json      optional  last run of each ingest job
    prices/<TICKER>.json    optional  price history for charts

Today the bundle is the placeholder fixture. When data_pipeline processes the
starter CSVs it writes the same files to a folder or S3 prefix, and nothing
else in the backend changes.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import FIXTURE_DIR, get_settings

REQUIRED_FILES = ("assets.json", "asset_metrics.json")
OPTIONAL_FILES = ("correlations.json", "signals.json", "macro_snapshot.json", "data_quality_report.json",
                  "asset_features.json", "fundamentals.json", "live_extension.json", "ingest_status.json")

# Assumption used only when correlations.json is missing. Values are rough,
# documented priors by asset group, not measurements. Confidence is lowered
# whenever they are used.
_GROUP_CORR = {
    frozenset(["equity"]): 0.55,
    frozenset(["equity", "broad_equity_fund"]): 0.75,
    frozenset(["broad_equity_fund"]): 0.85,
    frozenset(["equity", "bond"]): 0.15,
    frozenset(["broad_equity_fund", "bond"]): 0.15,
    frozenset(["equity", "gold"]): 0.10,
    frozenset(["broad_equity_fund", "gold"]): 0.10,
    frozenset(["equity", "crypto"]): 0.35,
    frozenset(["broad_equity_fund", "crypto"]): 0.40,
    frozenset(["bond"]): 0.70,
    frozenset(["bond", "gold"]): 0.30,
    frozenset(["bond", "crypto"]): 0.10,
    frozenset(["gold", "crypto"]): 0.15,
    frozenset(["crypto"]): 0.80,
    frozenset(["gold"]): 0.90,
}
HEURISTIC_CORR_NOTE = (
    "Correlations are estimated from asset-group priors (correlations.json not loaded); "
    "values are assumptions, not measurements."
)


class DataBundleError(RuntimeError):
    pass


class DataProvider:
    """Read-only view over one curated bundle."""

    def __init__(self, files: dict[str, Any], source_label: str, prices_loader=None):
        missing = [f for f in REQUIRED_FILES if f not in files]
        if missing:
            raise DataBundleError(f"curated bundle {source_label} is missing {missing}")
        self.source_label = source_label
        self._files = files
        self._prices_loader = prices_loader
        self._prices_cache: dict[str, dict | None] = {}
        self._sparklines: dict[int, dict] = {}
        self.assets: dict[str, dict] = {a["ticker"]: a for a in files["assets.json"]["assets"]}
        self._metrics_doc = files["asset_metrics.json"]
        self._corr: dict[str, tuple[dict[str, int], list[list[float]]]] = {}
        for window, block in (files.get("correlations.json") or {}).get("windows", {}).items():
            idx = {t: i for i, t in enumerate(block["tickers"])}
            self._corr[window] = (idx, block["matrix"])
        self._features = files.get("asset_features.json") or {}
        c1 = self._features.get("correlation_1y") or {}
        self._corr_1y = ({t: i for i, t in enumerate(c1.get("tickers", []))}, c1.get("matrix") or [])
        self._live = files.get("live_extension.json") or {}
        self._signals_sorted = sorted((files.get("signals.json") or {}).get("signals", []),
                                      key=lambda s: s.get("published_at", ""), reverse=True)

    # ----- metadata -------------------------------------------------------
    @property
    def metrics_provenance(self) -> dict:
        return self._metrics_doc.get("provenance", {})

    @property
    def is_placeholder(self) -> bool:
        return self.metrics_provenance.get("kind") == "placeholder"

    @property
    def windows(self) -> list[str]:
        return list(self._metrics_doc.get("windows", {}).keys())

    def window_period(self, window: str) -> str:
        block = self._metrics_doc["windows"][window]
        return f"{block.get('start')} to {block.get('end')}"

    def stress_scenarios(self) -> dict[str, dict]:
        return self._metrics_doc.get("stress_scenarios", {})

    def resolve_window(self, requested: str | None) -> tuple[str, str | None]:
        """Return the window to use and a note if we had to fall back."""
        available = self.windows
        if not available:
            raise DataBundleError("asset_metrics.json has no windows")
        requested = requested or get_settings().default_window
        if requested in available:
            return requested, None
        chosen = available[0]
        return chosen, f"Window '{requested}' is not in the loaded data; using '{chosen}' ({self.window_period(chosen)})."

    # ----- metrics --------------------------------------------------------
    def metrics(self, ticker: str, window: str) -> dict | None:
        return self._metrics_doc["windows"].get(window, {}).get("metrics", {}).get(ticker)

    def metric(self, ticker: str, window: str, name: str):
        m = self.metrics(ticker, window)
        if not m:
            return None
        value = m.get(name)
        if isinstance(value, float) and math.isnan(value):
            return None
        return value

    def evidence_ref(self, ticker: str, window: str, name: str) -> str:
        return f"asset_metrics.json#windows.{window}.metrics.{ticker}.{name}"

    def correlation(self, a: str, b: str, window: str) -> tuple[float, bool]:
        """Return (correlation, is_heuristic)."""
        if a == b:
            return 1.0, False
        if window in self._corr:
            idx, matrix = self._corr[window]
            if a in idx and b in idx:
                return float(matrix[idx[a]][idx[b]]), False
        # Fall back to a measured correlation with SPY if one side is SPY.
        for x, y in ((a, b), (b, a)):
            if x == "SPY":
                c = self.metric(y, window, "corr_spy")
                if c is not None:
                    return float(c), False
        return self._heuristic_corr(a, b), True

    def has_correlation_matrix(self, window: str) -> bool:
        return window in self._corr

    def _heuristic_corr(self, a: str, b: str) -> float:
        ga, gb = self.assets[a]["asset_group"], self.assets[b]["asset_group"]
        base = _GROUP_CORR.get(frozenset([ga, gb]), 0.3)
        if ga == gb and ga in ("equity", "broad_equity_fund"):
            if self.assets[a]["region"] == self.assets[b]["region"]:
                base += 0.10
            if ga == "equity" and self.assets[a]["sector"] == self.assets[b]["sector"]:
                base += 0.15
        return min(base, 0.95)

    # ----- features (asset_features.json) ---------------------------------
    @property
    def has_features(self) -> bool:
        return bool(self._features.get("assets"))

    @property
    def features_as_of(self) -> str | None:
        return self._features.get("as_of")

    @property
    def data_through(self) -> dict:
        return self._features.get("data_through") or {}

    def features(self, ticker: str) -> dict:
        return ((self._features.get("assets") or {}).get(ticker) or {}).get("values") or {}

    def feature(self, ticker: str, key: str):
        v = self.features(ticker).get(key)
        return None if isinstance(v, float) and math.isnan(v) else v

    def feature_missing(self, ticker: str) -> dict:
        return ((self._features.get("assets") or {}).get(ticker) or {}).get("missing") or {}

    def feature_details(self, ticker: str) -> dict:
        return ((self._features.get("assets") or {}).get(ticker) or {}).get("details") or {}

    def feature_definitions(self) -> dict:
        return self._features.get("definitions") or {}

    def feature_groups(self) -> dict:
        return self._features.get("groups") or {}

    def features_unavailable(self) -> dict:
        return self._features.get("unavailable") or {}

    def feature_ref(self, ticker: str, key: str) -> str:
        return f"asset_features.json#assets.{ticker}.values.{key}"

    def features_provenance(self) -> dict:
        return self._features.get("provenance") or {}

    def market_regime(self) -> dict | None:
        return self._features.get("market_regime")

    def correlation_1y(self, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        idx, m = self._corr_1y
        if a in idx and b in idx:
            v = m[idx[a]][idx[b]]
            return None if v is None else float(v)
        return None

    # ----- signals, macro, prices ----------------------------------------
    def signals(self, tickers: list[str] | None = None, days: int | None = None) -> list[dict]:
        items = self._signals_sorted
        if days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            items = [s for s in items if parse_ts(s.get("published_at")) >= cutoff]
        if tickers is not None:
            wanted = set(tickers)
            items = [s for s in items if wanted.intersection(s.get("tickers", []))]
        return list(items)

    def signals_between(self, tickers: list[str] | None, start: str, end: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
        """Signals published from start to end (ISO dates, inclusive), newest first."""
        wanted = set(tickers) if tickers is not None else None
        out = []
        for s in self._signals_sorted:
            d = (s.get("published_at") or "")[:10]
            if not d or d > end:
                continue
            if d < start:
                break
            if kinds and s.get("kind") not in kinds:
                continue
            if wanted is not None and not wanted.intersection(s.get("tickers", [])):
                continue
            out.append(s)
        return out

    def macro_signals(self, start: str, end: str) -> list[dict]:
        """Central-bank releases and policy-rate decisions. Decisions read from the rate series are
        always available (asset_features.json), even before the official_rss ingest job runs."""
        items = self.signals_between(None, start, end, kinds=("macro", "official"))
        have = {s["signal_id"] for s in items}
        for s in self._features.get("policy_decisions") or []:
            if s["signal_id"] not in have and start <= s["published_at"][:10] <= end:
                items.append(s)
        return sorted(items, key=lambda s: s.get("published_at", ""), reverse=True)

    def macro_snapshot(self) -> dict | None:
        return self._files.get("macro_snapshot.json")

    def ingest_status(self) -> dict | None:
        return self._files.get("ingest_status.json")

    def price_history(self, ticker: str) -> dict | None:
        """Starter price file, plus the rows of the live extension (marked) when it is loaded."""
        if ticker not in self._prices_cache:
            doc = self._prices_loader(ticker) if self._prices_loader else None
            ext = ((self._live.get("prices") or {}).get(ticker) or {}).get("points") or []
            if doc and ext:
                last = doc["points"][-1]["date"] if doc.get("points") else ""
                extra = [{**p, "live": True} for p in ext if p["date"] > last]
                doc = {**doc, "points": doc["points"] + extra,
                       "live_extension": {"rows": len(extra), "through": extra[-1]["date"] if extra else None,
                                          "splice": (self._live["prices"][ticker] or {}).get("splice"),
                                          "source": (self._live.get("sources") or {}).get("prices")}}
            self._prices_cache[ticker] = doc
        return self._prices_cache[ticker]

    def sparklines(self, max_points: int = 80) -> dict:
        """Downsampled adj_close per asset for small trend charts (listing currency).

        Points are picked at evenly spaced positions, always keeping the first and
        last observation, so the start and end values are exact.
        """
        if max_points not in self._sparklines:
            series = {}
            for ticker in self.assets:
                doc = self.price_history(ticker)
                points = (doc or {}).get("points") or []
                if len(points) < 2:
                    continue
                n = len(points)
                idx = range(n) if n <= max_points else sorted({round(i * (n - 1) / (max_points - 1)) for i in range(max_points)})
                series[ticker] = {
                    "currency": doc.get("currency"),
                    "start": points[0]["date"],
                    "end": points[-1]["date"],
                    "first": points[0]["adj_close"],
                    "last": points[-1]["adj_close"],
                    "values": [points[i]["adj_close"] for i in idx],
                }
            self._sparklines[max_points] = {
                "field": "adj_close",
                "max_points": max_points,
                "series": series,
                "missing": [t for t in self.assets if t not in series],
            }
        return self._sparklines[max_points]

    def status(self) -> dict:
        present = [f for f in REQUIRED_FILES + OPTIONAL_FILES if f in self._files]
        return {
            "source": self.source_label,
            "is_placeholder": self.is_placeholder,
            "files_present": present,
            "files_missing": [f for f in REQUIRED_FILES + OPTIONAL_FILES if f not in self._files],
            "windows": {w: self.window_period(w) for w in self.windows},
            "asset_count": len(self.assets),
            "provenance": {
                "assets": self._files["assets.json"].get("provenance", {}),
                "asset_metrics": self.metrics_provenance,
                "signals": (self._files.get("signals.json") or {}).get("provenance"),
                "asset_features": self.features_provenance() or None,
                "fundamentals": (self._files.get("fundamentals.json") or {}).get("provenance"),
                "live_extension": (self._live or {}).get("provenance"),
            },
            "features": {"as_of": self.features_as_of, "data_through": self.data_through,
                         "unavailable": self.features_unavailable(),
                         "definitions": self.feature_definitions(), "groups": self.feature_groups()} if self.has_features else None,
            "live_extension": {k: self._live.get(k) for k in ("fetched_at", "through", "sources", "errors")} if self._live else None,
            "ingest_status": self.ingest_status(),
            "signals_count": len(self._signals_sorted),
            "data_quality_report": self._files.get("data_quality_report.json"),
        }


def parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# ----- loaders ------------------------------------------------------------
def load_from_dir(path: Path) -> DataProvider:
    files = {}
    for name in REQUIRED_FILES + OPTIONAL_FILES:
        p = path / name
        if p.exists():
            files[name] = json.loads(p.read_text())

    def prices(ticker: str):
        p = path / "prices" / f"{ticker}.json"
        return json.loads(p.read_text()) if p.exists() else None

    return DataProvider(files, f"dir:{path}", prices)


def load_from_s3(uri: str) -> DataProvider:
    import boto3  # available in the Lambda runtime

    bucket, _, prefix = uri[len("s3://"):].partition("/")
    prefix = prefix.rstrip("/")
    s3 = boto3.client("s3")

    def read(key: str):
        try:
            obj = s3.get_object(Bucket=bucket, Key=f"{prefix}/{key}" if prefix else key)
        except s3.exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    files = {name: doc for name in REQUIRED_FILES + OPTIONAL_FILES if (doc := read(name)) is not None}
    return DataProvider(files, uri, lambda t: read(f"prices/{t}.json"))


def _load_provider() -> DataProvider:
    source = get_settings().data_source
    if source == "fixture":
        return load_from_dir(FIXTURE_DIR)
    if source.startswith("dir:"):
        return load_from_dir(Path(source[4:]))
    if source.startswith("s3://"):
        return load_from_s3(source)
    raise DataBundleError(f"Unknown ALPHAFORGE_DATA_SOURCE: {source}")


_CACHE: dict = {}


def get_provider() -> DataProvider:
    """One provider per process (per warm Lambda container), reloaded after
    ALPHAFORGE_DATA_TTL_SECONDS (default 900) so ingest updates reach a running API."""
    ttl = float(os.environ.get("ALPHAFORGE_DATA_TTL_SECONDS") or 900)
    now = time.monotonic()
    if "provider" not in _CACHE or now - _CACHE["at"] > ttl:
        _CACHE["provider"], _CACHE["at"] = _load_provider(), now
    return _CACHE["provider"]


get_provider.cache_clear = _CACHE.clear  # same interface as functools.lru_cache, used by tests
