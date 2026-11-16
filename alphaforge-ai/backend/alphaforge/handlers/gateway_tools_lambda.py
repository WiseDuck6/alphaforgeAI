"""Lambda target for Amazon Bedrock AgentCore Gateway (the advisor agent's tools).

Handler setting in Lambda: alphaforge.handlers.gateway_tools_lambda.handler
Tool schema for the Gateway target: agents/gateway_tools.json

Gateway passes the tool arguments as `event` and the tool name in
context.client_context.custom["bedrockAgentCoreToolName"] as
"<target_name>___<tool_name>".
"""

from __future__ import annotations

import json
import logging

from ..agent_tools import execute_session_tool
from ..data.provider import get_provider
from ..storage import get_store

log = logging.getLogger()
log.setLevel(logging.INFO)
DELIMITER = "___"


def _tool_name(context) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName", "")
    return name.split(DELIMITER, 1)[1] if DELIMITER in name else name


def handler(event, context):
    name = _tool_name(context)
    status, text = execute_session_tool(name, event or {}, get_store(), get_provider())
    log.info("tool=%s status=%s bytes=%d", name, status, len(text))
    if text.endswith('..."(truncated)"'):
        return {"result_truncated": text}
    return json.loads(text)
