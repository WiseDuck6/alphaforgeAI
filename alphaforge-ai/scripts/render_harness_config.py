"""Fill agents/harness_config.json and write dist/harness_config.json for create-harness.

    python3 scripts/render_harness_config.py --account 123456789012 --region us-east-1 \
        --gateway-id <gateway-id> --model-id <inference-profile-id>
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--gateway-id", required=True)
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--role-name", default="alphaforge-harness-role")
    a = ap.parse_args()

    cfg = json.loads((ROOT / "agents" / "harness_config.json").read_text())
    cfg.pop("_comment", None)
    cfg["executionRoleArn"] = f"arn:aws:iam::{a.account}:role/{a.role_name}"
    cfg["model"]["bedrockModelConfig"]["modelId"] = a.model_id
    cfg["systemPrompt"] = [{"text": (ROOT / "agents" / "system_prompt.md").read_text()}]
    cfg["tools"][0]["config"]["agentCoreGateway"]["gatewayArn"] = (
        f"arn:aws:bedrock-agentcore:{a.region}:{a.account}:gateway/{a.gateway_id}")
    out = ROOT / "dist" / "harness_config.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(cfg, indent=1))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
