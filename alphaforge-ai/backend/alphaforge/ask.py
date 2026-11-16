"""'Ask AlphaForge': answers in a fixed explainability contract.

Two modes share one output shape:
  agent  Amazon Bedrock AgentCore Harness (managed agent loop) calling the engines through
         AgentCore Gateway -> gateway_tools Lambda. Around the LLM, code enforces: Bedrock
         Guardrails on input and output, a deterministic governance check for allocation
         requests, and a check that every number in the answer appears in a tool output.
  local  No LLM. The reasoning engine (reasoning.py) plans the question, calls the same
         engines and writes the answer with numbered citations. Used for development,
         tests, and as the fallback when no harness is configured.

Contract:
  {mode, question, answer, intent, reasoning[{step, title, summary, details[], refs[], tool}],
   evidence[{ref, claim, value, unit, source_id, period, kind}], news[], assumptions[],
   limitations[], confidence{level, reasons}, trace[], trace_id, notice}

`reasoning` summarises the observable steps (what was looked at, which rule decided), with
references into `evidence`. It is not the model's internal chain of thought, which the
brief asks not to expose.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from . import SIMULATION_NOTICE
from .data.provider import DataProvider
from .governance import check_proposal
from .reasoning import PCT_RE, answer as reason, find_tickers  # noqa: F401  (find_tickers re-exported)


def _contract(mode: str, question: str, answer: str, evidence=None, assumptions=None, limitations=None,
              confidence=None, trace=None, **extra) -> dict:
    return {
        "mode": mode,
        "question": question,
        "answer": answer,
        "evidence": evidence or [],
        "assumptions": assumptions or [],
        "limitations": limitations or [],
        "confidence": confidence or {"level": "n/a", "reasons": []},
        "trace": trace or [],
        "trace_id": str(uuid.uuid4()),
        "notice": SIMULATION_NOTICE,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }


def answer_local(question: str, profile: dict, provider: DataProvider, window: str | None = None) -> dict:
    r = reason(question, profile, provider, window)
    core = {k: r.pop(k) for k in ("answer", "evidence", "assumptions", "limitations", "confidence", "trace")}
    return _contract("local", question, core["answer"], core["evidence"], core["assumptions"], core["limitations"],
                     core["confidence"], core["trace"], **r)


# ----- Agent mode: Amazon Bedrock AgentCore Harness ------------------------
SESSION_TTL_SECONDS = 900
NUM_RE = re.compile(r"(?<![\w.\[])(-?\d+(?:\.\d+)?)(\s*%|/100)?(?!\])")


def _reasoning_from_trace(calls: list[dict], parsed: dict) -> list[dict]:
    """Reasoning steps for an agent answer: the model's own short step summaries if it gave
    them (verified against tool outputs like the answer), else one step per tool call."""
    given = parsed.get("reasoning") or parsed.get("reasoning_summary") or []
    if isinstance(given, list) and given and all(isinstance(s, dict) for s in given):
        return [{"step": i + 1, "title": str(s.get("title") or f"Step {i + 1}")[:120],
                 "summary": str(s.get("summary") or "")[:600], "details": [str(d)[:400] for d in (s.get("details") or [])][:8],
                 "refs": [int(x) for x in (s.get("refs") or []) if isinstance(x, (int, float))], "tool": s.get("tool")}
                for i, s in enumerate(given[:10])]
    titles = {"get_profile_summary": "Read the investor profile", "get_profile_features": "Read the investor profile",
              "get_asset_features": "Gather market evidence", "get_asset_metrics": "Gather market evidence",
              "get_news": "Gather news evidence", "explain_price_move": "Match news to price moves",
              "get_signals": "Gather news evidence", "score_assets": "Rank the universe", "explain_asset_fit": "Evaluate fit",
              "check_constraints": "Run the governance check", "get_portfolio_radar": "Compare portfolio and target",
              "get_suggestions": "Compare portfolio and target", "run_stress": "Replay the scenario",
              "get_market_regime": "Read market conditions"}
    return [{"step": i + 1, "title": titles.get(c["tool"], c["tool"]), "summary": f"Called {c['tool']} ({c['status']}).",
             "details": [f"input: {json.dumps(c['input'])[:300]}"], "refs": [], "tool": c["tool"]} for i, c in enumerate(calls)]


def answer_agent(question: str, user_id: str, profile: dict, provider: DataProvider, settings, store) -> dict:
    """Ask the advisor harness, then verify and wrap its answer in the contract."""
    import secrets

    import boto3

    # 1. Guardrail on the question (denied topics: personalized advice, real trades; PII).
    if settings.guardrail_id:
        g = _apply_guardrail(question, "INPUT", settings)
        if g["intervened"]:
            return _contract("agent", question, g["text"], trace=[{"step": 1, "tool": "guardrail", "input": "question",
                                                                     "output": {"action": "GUARDRAIL_INTERVENED"}}],
                             limitations=["The question was blocked by the responsible-use guardrail."])

    # 2. Governance runs in code for allocation requests, whatever the model decides to do.
    governance = None
    tickers, pct = find_tickers(question, provider), PCT_RE.search(question)
    if tickers and pct:
        governance = check_proposal(profile, {"type": "add", "ticker": tickers[0],
                                              "weight": float(pct.group(1).replace(",", ".")) / 100}, provider)

    # 3. Bind this question to the authenticated user with a random, short-lived key.
    key = secrets.token_urlsafe(24)
    store.put("agent_sessions", key, {"user_id": user_id, "calls": []}, ttl_seconds=SESSION_TTL_SECONDS)

    # Region comes from the harness ARN, so the API can run in another Region. Credentials come
    # from the default chain (Lambda role, or `aws login` locally): no API key is used or stored.
    client = boto3.client("bedrock-agentcore", region_name=settings.harness_arn.split(":")[3])
    text, stop_reason, errors, budget_reached = _run_harness(client, settings, question, key, store, provider)

    calls = (store.get("agent_sessions", key) or {}).get("calls", [])
    trace = [{"step": i + 1, "tool": c["tool"], "input": c["input"], "output": f"{c['status']}: {c['output'][:300]}"}
             for i, c in enumerate(calls)]
    parsed = _extract_json(text) or {}
    answer = parsed.get("answer") or text.strip() or "The agent returned no answer."

    # 4. Guardrail on the answer.
    if settings.guardrail_id:
        g = _apply_guardrail(answer, "OUTPUT", settings)
        if g["intervened"]:
            answer = g["text"]
            trace.append({"step": len(trace) + 1, "tool": "guardrail", "input": "answer",
                          "output": {"action": "GUARDRAIL_INTERVENED"}})

    # 5. Every number in the answer and in the reasoning summaries must come from a tool output.
    reasoning = _reasoning_from_trace(calls, parsed)
    outputs = [c["output"] for c in calls] + ([json.dumps(governance)] if governance else [])
    checked_text = answer + "\n" + "\n".join(s["summary"] + " " + " ".join(s["details"]) for s in reasoning
                                             if parsed.get("reasoning") or parsed.get("reasoning_summary"))
    verification = verify_numbers(checked_text, outputs)
    limitations = list(parsed.get("limitations") or [])
    confidence = parsed.get("confidence") or {"level": "low", "reasons": ["The agent did not report confidence."]}
    if verification["unmatched"]:
        limitations.append("Figures not found in any tool output: " + ", ".join(verification["unmatched"]) + ".")
        confidence = {"level": "low", "reasons": list(confidence.get("reasons", [])) + ["Unverified figures in the answer."]}
    if not parsed:
        limitations.append("The agent did not return the structured contract; see the trace for evidence.")
    if stop_reason not in (None, "end_turn"):
        limitations.append(f"The agent stopped early ({stop_reason}).")
    elif budget_reached:
        limitations.append(f"The agent reached its tool limit ({MAX_TOOL_ROUNDS} rounds) and answered from the "
                           "evidence gathered up to then.")
    if errors:
        limitations.append(f"Agent stream errors: {', '.join(errors)}.")
    if provider.is_placeholder:
        confidence = {"level": "low", "reasons": list(confidence.get("reasons", [])) + ["Metrics are placeholder values."]}
    evidence = [{**e, "ref": e.get("ref") or i + 1} for i, e in enumerate(parsed.get("evidence") or []) if isinstance(e, dict)]
    news = [e for e in evidence if str(e.get("source_id", "")).startswith("http")]
    return _contract("agent", question, answer, evidence, parsed.get("assumptions"), limitations,
                     confidence, trace, governance=governance, verification=verification, reasoning=reasoning,
                     news=[{"title": e.get("claim"), "source_url": e.get("source_id"), "published_at": None,
                            "publisher": e.get("period"), "ref": e["ref"]} for e in news],
                     intent=parsed.get("intent"))


MAX_TOOL_ROUNDS = 12  # rounds in which tool calls run (inline mode); one round may hold several calls
WRAP_UP_WARNING_ROUNDS = 2  # warn the model this many rounds before the limit
_WRAP_UP_NOTICE = ("Tool budget reached: no more tools will run for this question. Reply now with the JSON "
                   "object, using only the tool results you already have. Name anything you could not "
                   "check in limitations.")
_NOT_RUN = "Not run: the tool budget for this question is used up. Answer with the evidence you already have."
_STREAM_ERRORS = ("validationException", "internalServerException", "runtimeClientError")


def system_prompt() -> str:
    """The advisor instructions (agents/system_prompt.md). The Lambda package ships a copy
    next to this module; a local checkout reads the repository file."""
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for p in (here / "system_prompt.md", here.parent.parent / "agents" / "system_prompt.md"):
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError("agents/system_prompt.md not found; run scripts/package_lambda.sh to bundle it")


def _read_stream(response) -> dict:
    """Collect the answer text, the tool calls the harness handed back, the stop reason and errors."""
    text, uses, by_index, stop, errors = "", {}, {}, None, []
    for event in response["stream"]:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tu = start["toolUse"]
                by_index[event["contentBlockStart"].get("contentBlockIndex")] = tu["toolUseId"]
                uses[tu["toolUseId"]] = {"name": tu.get("name", ""), "input": ""}
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            text += delta.get("text", "")
            if "toolUse" in delta:
                tid = by_index.get(event["contentBlockDelta"].get("contentBlockIndex"))
                if tid in uses:
                    uses[tid]["input"] += delta["toolUse"].get("input", "")
        elif "messageStop" in event:
            stop = event["messageStop"].get("stopReason")
        else:
            errors += [f"{e}: {str(event[e].get('message', ''))[:200]}" for e in _STREAM_ERRORS if e in event]
    return {"text": text, "uses": uses, "stop": stop, "errors": errors}


def _run_harness(client, settings, question: str, key: str, store, provider) -> tuple[str, str | None, list[str], bool]:
    """One question through the AgentCore Harness. Returns (text, stop reason, stream errors,
    whether the tool budget was reached).

    inline mode: the AlphaForge tools are sent as inline functions with this call, and only
    they are allowed (no shell, files or browser). When the harness stops with
    stopReason=tool_use, the tools run here under the session key and their results go
    back in a follow-up InvokeHarness call on the same runtimeSessionId, until the model ends.
    Tools run for at most MAX_TOOL_ROUNDS rounds. The model is warned shortly before the limit;
    at the limit it is told to answer with what it has, and any further tool requests get a
    "not run" result, so it still writes an answer instead of being cut off.
    gateway mode: the harness's own Gateway tools are used and the call returns once.
    """
    from .agent_tools import execute_session_tool, inline_tool_specs

    inline = settings.agent_tools == "inline"
    common = {"harnessArn": settings.harness_arn,
              "runtimeSessionId": f"{uuid.uuid4()}-{uuid.uuid4().hex[:8]}"}  # at least 33 characters
    if inline:
        tools = inline_tool_specs()
        common.update(tools=tools, allowedTools=[f"@{t['name']}" for t in tools],
                      systemPrompt=[{"text": system_prompt()}], maxIterations=MAX_TOOL_ROUNDS + 2,
                      timeoutSeconds=settings.agent_timeout_seconds)
    if settings.agent_model_id:
        # No temperature: newer Claude models reject non-default values when thinking is adaptive.
        common["model"] = {"bedrockModelConfig": {"modelId": settings.agent_model_id, "maxTokens": 4096}}
    messages = [{"role": "user", "content": [{"text": question}, {"text": f"context_key: {key}"}]}]
    final_text, errors, budget_reached = "", [], False
    # Calls 0..MAX-1 may run tools. Call MAX is the model's answer after the wrap-up notice;
    # call MAX+1 is a last chance if it asked for tools anyway (they are not run).
    for call in range(MAX_TOOL_ROUNDS + 2):
        r = _read_stream(client.invoke_harness(messages=messages, **common))
        errors += r["errors"]
        if r["text"].strip():
            final_text = r["text"]
        if not inline or r["stop"] != "tool_use" or not r["uses"]:
            return final_text, r["stop"], errors, budget_reached
        if call == MAX_TOOL_ROUNDS + 1:
            break
        run = call < MAX_TOOL_ROUNDS
        results = []
        for tool_use_id, u in r["uses"].items():
            if not run:  # every toolUse needs a toolResult, even when refused
                results.append({"toolResult": {"toolUseId": tool_use_id, "content": [{"text": _NOT_RUN}], "status": "error"}})
                continue
            try:
                args = json.loads(u["input"] or "{}")
            except json.JSONDecodeError:
                args = {}
            status, out = execute_session_tool(u["name"], args if isinstance(args, dict) else {}, store, provider)
            results.append({"toolResult": {"toolUseId": tool_use_id, "content": [{"text": out}],
                                           "status": "success" if status == "ok" else "error"}})
        left = MAX_TOOL_ROUNDS - (call + 1)
        if left <= 0:
            budget_reached = True
            results.append({"text": _WRAP_UP_NOTICE})
        elif left <= WRAP_UP_WARNING_ROUNDS:
            results.append({"text": f"Note: {left} tool round{'s' if left > 1 else ''} left for this question. "
                                    "Call only what you still need, then answer."})
        messages = [{"role": "user", "content": results}]
    return final_text, "max_tool_rounds", errors, True


def _apply_guardrail(text: str, source: str, settings) -> dict:
    import boto3

    res = boto3.client("bedrock-runtime").apply_guardrail(
        guardrailIdentifier=settings.guardrail_id, guardrailVersion=settings.guardrail_version,
        source=source, content=[{"text": {"text": text}}],
    )
    intervened = res.get("action") == "GUARDRAIL_INTERVENED"
    out = " ".join(o.get("text", "") for o in res.get("outputs", [])) if intervened else text
    return {"intervened": intervened, "text": out}


def _numbers_in(obj, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    elif isinstance(obj, str):
        out.extend(float(m.group(1)) for m in NUM_RE.finditer(obj))


def verify_numbers(answer: str, tool_outputs: list[str]) -> dict:
    """Match each percentage, decimal and x/100 score in the answer to a tool output value.

    A percentage p matches a value v if v*100 (or v itself) rounds to p at the
    precision shown; decimals likewise. Plain integers (years, counts) and citation
    markers like [3] are not checked.
    """
    values: list[float] = []
    for raw in tool_outputs:
        try:
            _numbers_in(json.loads(raw), values)
        except (json.JSONDecodeError, TypeError):
            _numbers_in(raw, values)
    checked, unmatched = 0, []
    for m in NUM_RE.finditer(answer):
        raw, suffix = m.group(1), (m.group(2) or "").strip()
        num = float(raw)
        # Tolerance follows the displayed precision: "22%" covers 21.5-22.5, "22.0%" covers 21.95-22.05.
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** -decimals + 1e-9
        if suffix == "%":
            ok = any(abs(v * 100 - num) <= tol or abs(v - num) <= tol or abs(abs(v) * 100 - abs(num)) <= tol for v in values)
        elif suffix == "/100":
            ok = any(abs(v - num) <= tol for v in values)
        elif decimals:
            ok = any(abs(v - num) <= tol or abs(v * 100 - num) <= tol for v in values)
        else:
            continue
        checked += 1
        if not ok:
            unmatched.append(m.group(0).strip())
    return {"checked": checked, "matched": checked - len(unmatched), "unmatched": unmatched,
            "method": "Each percentage, decimal and x/100 score in the answer is matched against values in the tool outputs."}


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None
