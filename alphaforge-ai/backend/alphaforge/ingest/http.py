"""Polite HTTP client for the ingest jobs (standard library only).

- One User-Agent for every request. SEC EDGAR requires a descriptive one with a contact
  address: set ALPHAFORGE_HTTP_USER_AGENT to "<organisation> <email>" before running.
- A minimum interval per host (SEC asks for at most 10 requests a second; GDELT for one
  request every 5 seconds).
- Retries with backoff on 429 and 5xx. Errors are raised as IngestHttpError so a job can
  record them and continue with the next source.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_UA = "AlphaForge-AI datathon prototype (set ALPHAFORGE_HTTP_USER_AGENT to 'org contact@email')"
HOST_INTERVAL = {
    "data.sec.gov": 0.2, "www.sec.gov": 0.2,
    "api.gdeltproject.org": 6.0,
    "news.google.com": 1.0,
    "query1.finance.yahoo.com": 0.5, "query2.finance.yahoo.com": 0.5,
}


class IngestHttpError(RuntimeError):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{status or 'error'} for {url}: {message}")
        self.url, self.status = url, status


class Http:
    def __init__(self, user_agent: str | None = None, timeout: float = 20.0, retries: int = 2):
        self.user_agent = user_agent or os.environ.get("ALPHAFORGE_HTTP_USER_AGENT") or DEFAULT_UA
        self.timeout = timeout
        self.retries = retries
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def _wait(self, host: str) -> None:
        gap = HOST_INTERVAL.get(host, 0.25)
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + gap
            if due > now:
                time.sleep(due - now)
            self._last[host] = time.monotonic()

    def get(self, url: str, headers: dict | None = None) -> bytes:
        host = urlsplit(url).netloc
        last_err: IngestHttpError | None = None
        for attempt in range(self.retries + 1):
            self._wait(host)
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip",
                                                       **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:  # noqa: S310 (allow-listed hosts)
                    self.calls += 1
                    data = res.read()
                    if res.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as e:
                last_err = IngestHttpError(url, e.code, e.reason or "")
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = IngestHttpError(url, None, str(e))
            time.sleep(min(30, 2 ** attempt * max(1.0, HOST_INTERVAL.get(host, 0.25))))
        raise last_err or IngestHttpError(url, None, "unknown error")

    def text(self, url: str, headers: dict | None = None) -> str:
        return self.get(url, headers).decode("utf-8", errors="replace")

    def json(self, url: str, headers: dict | None = None):
        raw = self.text(url, headers)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            # GDELT answers rate-limited calls with a plain-text message and status 200.
            raise IngestHttpError(url, 200, f"not JSON: {raw[:120]!r}") from e
