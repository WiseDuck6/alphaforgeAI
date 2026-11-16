#!/usr/bin/env bash
# Build dist/alphaforge-lambda.zip, used by the three Lambda functions (API, Gateway tools, ingest).
#
# The backend has no third-party dependencies, but it calls new AWS APIs
# (bedrock-agentcore InvokeHarness). The boto3 bundled in the Lambda runtime can
# lag behind, so a current boto3 is packaged with the code. Pass --no-boto3 to skip.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist"
BUILD="$OUT/build"
rm -rf "$BUILD" "$OUT/alphaforge-lambda.zip"
mkdir -p "$BUILD"

if [[ "${1:-}" != "--no-boto3" ]]; then
  if command -v uv >/dev/null; then
    uv pip install --quiet --target "$BUILD" --python-platform aarch64-manylinux2014 --python-version 3.12 boto3
  else
    python3 -m pip install --quiet --target "$BUILD" boto3
  fi
fi

cp -r "$ROOT/backend/alphaforge" "$BUILD/"
# The advisor instructions sent with each InvokeHarness call (inline tool mode).
cp "$ROOT/agents/system_prompt.md" "$BUILD/alphaforge/system_prompt.md"
cd "$BUILD"
zip -qr "$OUT/alphaforge-lambda.zip" . -x '*/__pycache__/*' '*.pyc'
echo "Built $OUT/alphaforge-lambda.zip ($(du -h "$OUT/alphaforge-lambda.zip" | cut -f1))"
