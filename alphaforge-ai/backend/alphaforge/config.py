"""Runtime settings, read from environment variables.

Local development and AWS Lambda use the same code; only these variables change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "fixtures"


@dataclass(frozen=True)
class Settings:
    # Where curated data comes from:
    #   "fixture"            -> bundled placeholder files (alphaforge/data/fixtures)
    #   "dir:/abs/path"      -> a local folder written by data_pipeline
    #   "s3://bucket/prefix" -> the same files in S3 (used by Lambda)
    data_source: str
    # "local" (JSON files on disk) or "dynamodb"
    storage: str
    local_store_dir: Path
    table_prefix: str
    # "header" (dev: X-User-Id header) or "cognito" (JWT claims from API Gateway)
    auth_mode: str
    default_window: str
    # Amazon Bedrock AgentCore Harness that runs the advisor agent (agent mode).
    harness_arn: str | None
    # How the harness reaches the AlphaForge tools:
    #   "inline"  -> tools are passed as inline functions on each InvokeHarness call and run
    #                in this process (no Gateway or tools Lambda needed)
    #   "gateway" -> the harness calls them through AgentCore Gateway -> gateway_tools Lambda
    agent_tools: str
    # Optional Bedrock model or inference-profile id that overrides the harness's model per call.
    agent_model_id: str | None
    agent_timeout_seconds: int
    # Optional Amazon Bedrock Guardrail applied to questions and answers.
    guardrail_id: str | None
    guardrail_version: str
    # "local" (record only) or "eventbridge" (call EventBridge Scheduler UpdateSchedule)
    scheduler_mode: str
    scheduler_group: str
    scheduler_name_prefix: str
    aws_region: str | None


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        data_source=_env("ALPHAFORGE_DATA_SOURCE", "fixture"),
        storage=_env("ALPHAFORGE_STORAGE", "local"),
        local_store_dir=Path(_env("ALPHAFORGE_LOCAL_STORE_DIR", str(BACKEND_DIR / ".localdata"))),
        table_prefix=_env("ALPHAFORGE_TABLE_PREFIX", "alphaforge-"),
        auth_mode=_env("ALPHAFORGE_AUTH_MODE", "header"),
        default_window=_env("ALPHAFORGE_DEFAULT_WINDOW", "3y"),
        harness_arn=_env("AGENTCORE_HARNESS_ARN"),
        agent_tools=_env("ALPHAFORGE_AGENT_TOOLS", "inline"),
        agent_model_id=_env("AGENTCORE_MODEL_ID"),
        agent_timeout_seconds=int(_env("AGENTCORE_TIMEOUT_SECONDS", "120")),
        guardrail_id=_env("BEDROCK_GUARDRAIL_ID"),
        guardrail_version=_env("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
        scheduler_mode=_env("ALPHAFORGE_SCHEDULER", "local"),
        scheduler_group=_env("ALPHAFORGE_SCHEDULER_GROUP", "alphaforge-ingest"),
        scheduler_name_prefix=_env("ALPHAFORGE_SCHEDULER_PREFIX", "alphaforge-ingest-"),
        aws_region=_env("AWS_REGION"),
    )
