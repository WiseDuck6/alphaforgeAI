"""API through the Lambda handlers (API Gateway v2 events and Bedrock action-group events)."""

import json

from alphaforge.handlers import api_lambda, gateway_tools_lambda
from alphaforge.storage import get_store


def _event(method, path, body=None, user="robert", query=None, claims=None):
    ev = {"rawPath": path, "requestContext": {"http": {"method": method}},
          "headers": {"x-user-id": user}, "queryStringParameters": query,
          "body": json.dumps(body) if body is not None else None}
    if claims:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return ev


def call(*args, **kwargs):
    res = api_lambda.handler(_event(*args, **kwargs), None)
    return res["statusCode"], json.loads(res["body"])


def test_dashboard_for_demo_persona():
    status, body = call("GET", "/me/dashboard")
    assert status == 200
    assert len(body["radar"]["axes"]) == 9
    assert body["top_picks"] and body["data_status"]["is_placeholder"]


def test_risk_level_what_if_changes_scores():
    _, base = call("GET", "/assets", query={})
    _, high = call("GET", "/assets", query={"risk_level": "high"})
    s = lambda b: {r["ticker"]: r["score"] for r in b["results"]}["NVDA"]
    assert s(high) > s(base)


def test_profile_validation_error_is_400():
    status, body = call("PUT", "/me/profile", {"risk_level": "yolo", "horizon_years": 5}, user="newuser")
    assert status == 400 and "risk_level" in body["error"]


def test_unknown_user_without_profile_is_404():
    status, _ = call("GET", "/me/dashboard", user="nobody")
    assert status == 404


def test_ask_local_mode_returns_contract_and_stores_trace():
    status, body = call("POST", "/ask", {"question": "Should I put 30% of my portfolio in Tesla?"})
    assert status == 200 and body["mode"] == "local"
    for key in ("answer", "evidence", "assumptions", "limitations", "confidence", "trace", "notice"):
        assert key in body
    status, trace = call("GET", f"/traces/{body['trace_id']}")
    assert status == 200 and trace["question"].startswith("Should I")
    status, _ = call("GET", f"/traces/{body['trace_id']}", user="amira")
    assert status == 404  # traces are private to their user


def test_source_frequency_update_local():
    status, body = call("PUT", "/sources/gdelt", {"rate": "15m"})
    assert status == 200 and body["scheduler"]["applied"] is False
    _, listing = call("GET", "/sources")
    assert next(s for s in listing["sources"] if s["source_id"] == "gdelt")["rate"] == "15m"
    status, _ = call("PUT", "/sources/gdelt", {"rate": "every second"})
    assert status == 400


def test_cognito_mode_requires_claims(monkeypatch):
    from alphaforge.config import get_settings

    monkeypatch.setenv("ALPHAFORGE_AUTH_MODE", "cognito")
    get_settings.cache_clear()
    status, _ = call("GET", "/me/profile")
    assert status == 401
    status, _ = call("POST", "/me/profile/preset/mateo", claims={"sub": "abc-123"})
    assert status == 200
    status, _ = call("PUT", "/sources/gdelt", {"rate": "1h"}, claims={"sub": "abc-123"})
    assert status == 403


class _Ctx:
    """Mimics the Lambda context AgentCore Gateway sends (client_context.custom)."""

    def __init__(self, tool):
        self.client_context = type("CC", (), {"custom": {"bedrockAgentCoreToolName": f"alphaforge-tools___{tool}"}})()


def test_gateway_tool_requires_valid_context_key_and_logs_calls():
    call("GET", "/me/profile")  # seeds the robert preset
    store = get_store()
    store.put("agent_sessions", "k1", {"user_id": "robert", "calls": []}, ttl_seconds=60)
    out = gateway_tools_lambda.handler({"context_key": "k1", "ticker": "TSLA", "weight": 0.3}, _Ctx("check_constraints"))
    assert out["status"] == "conflict"
    out = gateway_tools_lambda.handler({"context_key": "forged", "ticker": "TSLA", "weight": 0.3}, _Ctx("check_constraints"))
    assert "context_key" in out["error"]
    out = gateway_tools_lambda.handler({"context_key": "k1", "ticker": "NOPE"}, _Ctx("explain_asset_fit"))
    assert "unknown tickers" in out["error"]
    calls = store.get("agent_sessions", "k1")["calls"]
    assert [c["tool"] for c in calls] == ["check_constraints", "explain_asset_fit"]


def test_agent_mode_runs_as_job_with_verification(monkeypatch):
    """Simulates the AgentCore harness: it calls one Gateway tool, then streams a JSON answer."""
    import sys
    import types

    from alphaforge.config import get_settings

    class FakeAgentCore:
        def invoke_harness(self, harnessArn, runtimeSessionId, messages, **kw):
            assert len(runtimeSessionId) >= 33
            assert "tools" not in kw  # gateway mode uses the harness's own tools
            key = messages[0]["content"][1]["text"].split(": ")[1]
            res = gateway_tools_lambda.handler({"context_key": key, "ticker": "TSLA", "weight": 0.3}, _Ctx("check_constraints"))
            vol = res["after"]["volatility"]
            answer = {"answer": f"This conflicts with your profile: volatility would reach {vol:.1%}, and 99.9% is made up.",
                      "evidence": [], "assumptions": [], "limitations": [], "confidence": {"level": "medium", "reasons": []}}
            return {"stream": [{"contentBlockDelta": {"delta": {"text": json.dumps(answer)}}},
                               {"messageStop": {"stopReason": "end_turn"}}]}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name, **kw: FakeAgentCore()))
    monkeypatch.setenv("AGENTCORE_HARNESS_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/test")
    monkeypatch.setenv("ALPHAFORGE_AGENT_TOOLS", "gateway")
    get_settings.cache_clear()
    monkeypatch.setattr(api_lambda.routes, "_start_job", api_lambda.routes.run_ask_job)  # run inline

    status, body = call("POST", "/ask", {"question": "Should I put 30% of my portfolio in Tesla?"})
    assert status == 200 and body["status"] == "pending"
    status, done = call("GET", f"/ask/{body['job_id']}")
    assert done["status"] == "done" and done["mode"] == "agent"
    assert done["governance"]["status"] == "conflict"          # governance ran in code
    assert done["trace"][0]["tool"] == "check_constraints"     # server-side trace of the tool call
    assert done["verification"]["unmatched"] == ["99.9%"]      # the invented figure is caught
    assert done["confidence"]["level"] == "low"
    status, _ = call("GET", f"/ask/{body['job_id']}", user="amira")
    assert status == 404


def test_agent_inline_tools_round_trip(monkeypatch):
    """Inline mode: the harness hands a tool call back, the API runs it and resumes the same session."""
    import sys
    import types

    from alphaforge.config import get_settings

    sessions, seen = set(), []

    class FakeAgentCore:
        def invoke_harness(self, harnessArn, runtimeSessionId, messages, tools, allowedTools, systemPrompt, **kw):
            sessions.add(runtimeSessionId)
            assert allowedTools == [f"@{t['name']}" for t in tools] and "context_key" in systemPrompt[0]["text"]
            content = messages[0]["content"]
            if "text" in content[0]:  # first call: ask for check_constraints
                key = content[1]["text"].split(": ")[1]
                args = json.dumps({"context_key": key, "ticker": "TSLA", "weight": 0.3})
                return {"stream": [
                    {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "t1", "name": "check_constraints"}}}},
                    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": args}}}},
                    {"messageStop": {"stopReason": "tool_use"}}]}
            result = content[0]["toolResult"]
            assert result["toolUseId"] == "t1" and result["status"] == "success"
            seen.append(json.loads(result["content"][0]["text"])["status"])
            answer = {"answer": "This conflicts with your profile.", "evidence": [], "assumptions": [], "limitations": [],
                      "confidence": {"level": "medium", "reasons": []}}
            return {"stream": [{"contentBlockDelta": {"delta": {"text": json.dumps(answer)}}},
                               {"messageStop": {"stopReason": "end_turn"}}]}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name, **kw: FakeAgentCore()))
    monkeypatch.setenv("AGENTCORE_HARNESS_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/test")
    monkeypatch.setenv("ALPHAFORGE_AGENT_TOOLS", "inline")
    get_settings.cache_clear()
    monkeypatch.setattr(api_lambda.routes, "_start_job", api_lambda.routes.run_ask_job)

    _, body = call("POST", "/ask", {"question": "Should I put 30% of my portfolio in Tesla?"})
    _, done = call("GET", f"/ask/{body['job_id']}")
    assert done["status"] == "done" and done["mode"] == "agent"
    assert seen == ["conflict"] and len(sessions) == 1               # tool ran here; same session resumed
    assert [t["tool"] for t in done["trace"]] == ["check_constraints"]
    get_settings.cache_clear()


def test_verify_numbers_formats():
    from alphaforge.ask import verify_numbers

    outputs = [json.dumps({"vol": 0.2203, "score": 27, "corr": 0.43, "axis": 93.6})]
    v = verify_numbers("Vol 22.0%, fit 27/100, correlation 0.43, risk axis 93.6, in 2022, and 51%.", outputs)
    assert v["checked"] == 5 and v["unmatched"] == ["51%"]
