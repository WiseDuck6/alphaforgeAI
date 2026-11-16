"""Framework-free HTTP router shared by the Lambda handler and the local dev server."""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("alphaforge.api")


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    user_id: str | None = None
    claims: dict = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


Handler = Callable[[Request], Any]
_ROUTES: list[tuple[str, re.Pattern, Handler]] = []


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn: Handler) -> Handler:
        _ROUTES.append((method, regex, fn))
        return fn
    return deco


def dispatch(req: Request) -> tuple[int, dict]:
    path = req.path[4:] if req.path.startswith("/api/") else req.path
    path = path.rstrip("/") or "/"
    allowed = False
    for method, regex, fn in _ROUTES:
        m = regex.match(path)
        if not m:
            continue
        allowed = True
        if method != req.method:
            continue
        req.params = m.groupdict()
        try:
            return 200, fn(req)
        except HttpError as e:
            return e.status, {"error": e.message}
        except ValueError as e:  # validation errors from the engines
            return 400, {"error": str(e)}
        except Exception:  # noqa: BLE001 - return a generic 500, log details
            log.error("unhandled error on %s %s\n%s", req.method, path, traceback.format_exc())
            return 500, {"error": "internal error"}
    if allowed:
        return 405, {"error": "method not allowed"}
    return 404, {"error": f"no route for {req.method} {path}"}


# Sent with every API response (dev server and Lambda). Results depend on who is asking,
# so nothing between the browser and the API may store them.
NO_CACHE_HEADERS = {"cache-control": "no-store", "vary": "X-User-Id, Authorization"}


def parse_body(raw: str | bytes | None) -> Any:
    if raw in (None, b"", ""):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HttpError(400, "body must be valid JSON") from None
