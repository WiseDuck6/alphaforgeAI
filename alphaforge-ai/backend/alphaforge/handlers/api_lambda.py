"""AWS Lambda entry point for the HTTP API (API Gateway HTTP API, payload format 2.0).

Handler setting in Lambda: alphaforge.handlers.api_lambda.handler
"""

from __future__ import annotations

import base64
import json
import logging
import os

# Lambda defaults to secure auth unless explicitly told otherwise.
os.environ.setdefault("ALPHAFORGE_AUTH_MODE", "cognito")

from ..api import routes  # noqa: E402,F401  (registers routes)
from ..api.router import NO_CACHE_HEADERS, HttpError, Request, dispatch, parse_body  # noqa: E402

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    if "alphaforge_job" in event:  # asynchronous self-invocation for agent questions
        routes.run_ask_job(str(event["alphaforge_job"]))
        return {"ok": True}
    http = event.get("requestContext", {}).get("http", {})
    raw = event.get("body")
    if raw and event.get("isBase64Encoded"):
        raw = base64.b64decode(raw)
    claims = (event.get("requestContext", {}).get("authorizer", {}) or {}).get("jwt", {}).get("claims", {}) or {}
    try:
        body = parse_body(raw)
        req = Request(
            method=http.get("method", "GET").upper(),
            path=event.get("rawPath", "/"),
            query=event.get("queryStringParameters") or {},
            headers={k.lower(): v for k, v in (event.get("headers") or {}).items()},
            body=body,
            claims=claims,
        )
        status, payload = dispatch(req)
    except HttpError as e:
        status, payload = e.status, {"error": e.message}
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **NO_CACHE_HEADERS},
        "body": json.dumps(payload, default=str),
    }
