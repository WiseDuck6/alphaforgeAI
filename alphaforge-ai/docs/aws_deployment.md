# Deploying AlphaForge AI on AWS

This guide sets up the AWS side of the prototype, from the data bucket to the agent. Each step says what the service does in the solution, which is what the jury's AWS criterion asks for. Commands use the AWS CLI v2 in bash; the console works too.

Run everything from the repository root. Run the steps in order: later steps need ARNs from earlier ones.

```
EventBridge Scheduler ──► Lambda alphaforge-ingest ──► SEC EDGAR, Bank of Canada, FRED, Yahoo Finance, allow-listed news
                                │  writes signals, fundamentals, live prices, then rebuilds asset_features.json
                                ▼
Browser ── CloudFront + S3 (React app)
   │  sign-in: Amazon Cognito (JWT)
   ▼
API Gateway HTTP API (JWT authorizer) ──► Lambda alphaforge-api ──► DynamoDB (profiles, sources, traces, sessions, jobs)
                                             │   ├─► S3 curated/ (asset_features.json, asset_metrics.json, signals.json, ...)
                                             │   ├─► Bedrock Guardrails (ApplyGuardrail)
                                             │   ├─► EventBridge Scheduler (UpdateSchedule: monitoring frequency)
                                             │   └─► AgentCore Harness "alphaforge_advisor" (InvokeHarness)
                                             │            └─► AgentCore Gateway ──► Lambda alphaforge-tools ──► same engines + DynamoDB + S3
                                             └── invokes itself asynchronously for agent questions (API Gateway HTTP APIs time out after 30 s)
```

> **Why not classic Bedrock Agents?** Classic Amazon Bedrock Agents is in maintenance mode and closed to new customers ([announcement](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-classic-maintenance-mode.html)). AgentCore Harness is the managed agent loop AWS recommends for new agents. See `agents/instructions.md` for the agent design.

## 0. Prerequisites

```bash
aws --version                      # needs AWS CLI v2 recent enough for "bedrock-agentcore-control"
aws login                          # or your workshop credentials
export REGION=us-east-1            # AgentCore Harness and Gateway are also available in ca-central-1
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=$REGION
render() { sed -e "s/ACCOUNT_ID/$ACCOUNT/g" -e "s/REGION/$REGION/g" "$@"; }   # fills the IAM templates
```

- **Model access:** pick a Claude model available through an inference profile in your Region: `aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId"`. Save it as `MODEL_ID`.
- **Cost:** everything here is on-demand. The main costs are model tokens and harness sessions. Delete resources after the datathon (section 11).

## 1. S3 data bucket: the data lake

The engines read a "curated bundle" of JSON files (contract in `docs/data_contract.md`). Build it from the starter CSVs with `python3 data_pipeline/build_bundle.py`, then upload `data_pipeline/out/`. (Uploading `backend/alphaforge/data/fixtures/` instead gives the placeholder metrics.)

```bash
aws s3api create-bucket --bucket alphaforge-data-$ACCOUNT --region $REGION \
  $( [ "$REGION" != us-east-1 ] && echo --create-bucket-configuration LocationConstraint=$REGION )
aws s3api put-public-access-block --bucket alphaforge-data-$ACCOUNT \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
python3 data_pipeline/build_bundle.py
aws s3 cp data_pipeline/out/ s3://alphaforge-data-$ACCOUNT/curated/ --recursive
aws s3 cp ../alphaforge_starter/ s3://alphaforge-data-$ACCOUNT/raw/starter/ --recursive --exclude '*' --include '*.csv' --include '*.txt'
```

S3 encrypts new objects by default (SSE-S3). The raw starter CSVs go under `raw/starter/` in the same bucket for lineage.

## 2. DynamoDB tables: low-latency app state

| Table | Holds | TTL |
|---|---|---|
| `alphaforge-profiles` | one fictional investor profile per Cognito user | no |
| `alphaforge-sources` | monitoring frequency per source and change history | no |
| `alphaforge-agent_traces` | each answer with evidence and trace | no |
| `alphaforge-agent_sessions` | random keys binding an agent question to a user, plus tool-call log | `expires_at` |
| `alphaforge-ask_jobs` | asynchronous agent questions | `expires_at` |

```bash
for t in profiles sources agent_traces agent_sessions ask_jobs; do
  aws dynamodb create-table --table-name alphaforge-$t \
    --attribute-definitions AttributeName=pk,AttributeType=S --key-schema AttributeName=pk,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
done
aws dynamodb wait table-exists --table-name alphaforge-ask_jobs
for t in agent_sessions ask_jobs; do
  aws dynamodb update-time-to-live --table-name alphaforge-$t \
    --time-to-live-specification Enabled=true,AttributeName=expires_at
done
```

## 3. Lambda functions: API and agent tools

Both functions use the same zip, a Python package with no third-party runtime dependencies. A current boto3 is bundled so the new AgentCore APIs are available.

```bash
./scripts/package_lambda.sh                                   # -> dist/alphaforge-lambda.zip

# Roles (least privilege; policies are in infra/iam/)
aws iam create-role --role-name alphaforge-api-role --assume-role-policy-document file://infra/iam/lambda-trust.json
aws iam put-role-policy --role-name alphaforge-api-role --policy-name app \
  --policy-document "$(render infra/iam/api-policy.json)"
aws iam create-role --role-name alphaforge-tools-role --assume-role-policy-document file://infra/iam/lambda-trust.json
aws iam put-role-policy --role-name alphaforge-tools-role --policy-name app \
  --policy-document "$(render infra/iam/tools-policy.json)"
sleep 10   # IAM propagation

COMMON_ENV="ALPHAFORGE_DATA_SOURCE=s3://alphaforge-data-$ACCOUNT/curated,ALPHAFORGE_STORAGE=dynamodb,ALPHAFORGE_TABLE_PREFIX=alphaforge-"

aws lambda create-function --function-name alphaforge-api --runtime python3.12 --architectures arm64 \
  --handler alphaforge.handlers.api_lambda.handler --zip-file fileb://dist/alphaforge-lambda.zip \
  --role arn:aws:iam::$ACCOUNT:role/alphaforge-api-role --memory-size 512 --timeout 180 \
  --environment "Variables={$COMMON_ENV,ALPHAFORGE_AUTH_MODE=cognito,ALPHAFORGE_SCHEDULER=local}"

aws lambda create-function --function-name alphaforge-tools --runtime python3.12 --architectures arm64 \
  --handler alphaforge.handlers.gateway_tools_lambda.handler --zip-file fileb://dist/alphaforge-lambda.zip \
  --role arn:aws:iam::$ACCOUNT:role/alphaforge-tools-role --memory-size 512 --timeout 30 \
  --environment "Variables={$COMMON_ENV}"
```

The API timeout is 180 s because the same function also runs the asynchronous agent jobs; API Gateway still cuts HTTP requests at 30 s. To update code later: `aws lambda update-function-code --function-name alphaforge-api --zip-file fileb://dist/alphaforge-lambda.zip` (same for `alphaforge-tools`).

## 4. Cognito and API Gateway: sign-in and the HTTP front door

**Cognito user pool:** each user owns one profile, keyed by the token's `sub`.

```bash
POOL_ID=$(aws cognito-idp create-user-pool --pool-name alphaforge-users \
  --auto-verified-attributes email --username-attributes email --query UserPool.Id --output text)
CLIENT_ID=$(aws cognito-idp create-user-pool-client --user-pool-id $POOL_ID --client-name alphaforge-web \
  --no-generate-secret --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --query UserPoolClient.ClientId --output text)
aws cognito-idp create-group --user-pool-id $POOL_ID --group-name admin   # may change monitoring frequency
```

**HTTP API with a JWT authorizer.** HTTP API is cheaper and simpler than REST API and validates Cognito tokens natively.

```bash
API_ID=$(aws apigatewayv2 create-api --name alphaforge-api --protocol-type HTTP \
  --cors-configuration '{"AllowOrigins":["http://localhost:5173"],"AllowMethods":["GET","POST","PUT"],"AllowHeaders":["authorization","content-type"]}' \
  --query ApiId --output text)
INTEG_ID=$(aws apigatewayv2 create-integration --api-id $API_ID --integration-type AWS_PROXY \
  --integration-uri arn:aws:lambda:$REGION:$ACCOUNT:function:alphaforge-api --payload-format-version 2.0 \
  --query IntegrationId --output text)
AUTH_ID=$(aws apigatewayv2 create-authorizer --api-id $API_ID --name cognito --authorizer-type JWT \
  --identity-source '$request.header.Authorization' \
  --jwt-configuration "{\"Audience\":[\"$CLIENT_ID\"],\"Issuer\":\"https://cognito-idp.$REGION.amazonaws.com/$POOL_ID\"}" \
  --query AuthorizerId --output text)
aws apigatewayv2 create-route --api-id $API_ID --route-key 'ANY /{proxy+}' --target integrations/$INTEG_ID \
  --authorization-type JWT --authorizer-id $AUTH_ID
aws apigatewayv2 create-route --api-id $API_ID --route-key 'GET /health' --target integrations/$INTEG_ID
aws apigatewayv2 create-stage --api-id $API_ID --stage-name '$default' --auto-deploy \
  --default-route-settings ThrottlingBurstLimit=20,ThrottlingRateLimit=10
aws lambda add-permission --function-name alphaforge-api --statement-id apigw --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/*"
echo "API: https://$API_ID.execute-api.$REGION.amazonaws.com"
curl -s https://$API_ID.execute-api.$REGION.amazonaws.com/health
```

**Not built yet:** the frontend has no sign-in screen. The next build step adds Cognito sign-in (Amplify Auth, or managed login with authorization code + PKCE) and stores the ID token as `af_id_token`, which `frontend/src/api.js` already sends as `Authorization: Bearer`. Until then, the deployed API answers only `/health`. Every other route returns 401, as it should.

## 5. AgentCore Gateway: the engines as agent tools

The Gateway turns the `alphaforge-tools` Lambda into MCP tools the agent can discover. The tool schema is generated from the code (`agents/gateway_tools.json`).

```bash
aws iam create-role --role-name alphaforge-gateway-role \
  --assume-role-policy-document "$(render infra/iam/agentcore-trust.json | sed 's/RESOURCE_TYPE/gateway/')"
aws iam put-role-policy --role-name alphaforge-gateway-role --policy-name invoke-tools \
  --policy-document "$(render infra/iam/gateway-policy.json)"
sleep 10

GATEWAY_ID=$(aws bedrock-agentcore-control create-gateway --name alphaforge-gateway \
  --role-arn arn:aws:iam::$ACCOUNT:role/alphaforge-gateway-role \
  --protocol-type MCP --authorizer-type AWS_IAM --query gatewayId --output text)
aws bedrock-agentcore-control get-gateway --gateway-identifier $GATEWAY_ID --query status   # wait for READY

# Resource policy on the tools Lambda: only the gateway role may invoke it
aws lambda add-permission --function-name alphaforge-tools --statement-id agentcore-gateway \
  --action lambda:InvokeFunction --principal arn:aws:iam::$ACCOUNT:role/alphaforge-gateway-role

python3 - <<EOF > dist/target.json
import json
tools = json.load(open("agents/gateway_tools.json"))
print(json.dumps({"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:$REGION:$ACCOUNT:function:alphaforge-tools",
                                     "toolSchema": {"inlinePayload": tools}}}}))
EOF
aws bedrock-agentcore-control create-gateway-target --gateway-identifier $GATEWAY_ID --name alphaforge-tools \
  --target-configuration file://dist/target.json \
  --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]'
aws bedrock-agentcore-control list-gateway-targets --gateway-identifier $GATEWAY_ID   # wait for READY
```

The target name `alphaforge-tools` becomes the tool prefix (`alphaforge-tools___check_constraints`); the Lambda strips it.

## 6. AgentCore Harness: the advisor agent

The harness runs the reasoning and tool-call loop with the system prompt in `agents/system_prompt.md`. It may only use the Gateway tools (`allowedTools`), so the built-in shell and file tools are off. Execution limits are set explicitly.

```bash
aws iam create-role --role-name alphaforge-harness-role \
  --assume-role-policy-document "$(render infra/iam/agentcore-trust.json | sed 's/RESOURCE_TYPE/harness/')"
```

Give that role the [sample harness execution policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html) merged with `infra/iam/harness-policy-additions.json` (model invoke on your inference profile, `InvokeGateway` on your gateway). Keep model ARNs specific rather than `*`. Then:

```bash
python3 scripts/render_harness_config.py --account $ACCOUNT --region $REGION --gateway-id $GATEWAY_ID --model-id $MODEL_ID
aws bedrock-agentcore-control create-harness --cli-input-json file://dist/harness_config.json
aws bedrock-agentcore-control list-harnesses                                   # note the harnessId and ARN
aws bedrock-agentcore-control get-harness --harness-id <harnessId> --query status   # repeat until READY
HARNESS_ARN=<harness ARN from get-harness>
```

Check that `allowedTools: ["@alphaforge-gateway"]` exposes exactly the nine AlphaForge tools: in the AgentCore console, open the harness and test it. If the selector format differs, adjust it using the [harness tools page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html).

Connect the API to the harness, then narrow the `AdvisorAgent` statement in `infra/iam/api-policy.json` from `...:*` to the harness ARN and its runtime ARN:

```bash
aws lambda update-function-configuration --function-name alphaforge-api --environment \
  "Variables={$COMMON_ENV,ALPHAFORGE_AUTH_MODE=cognito,ALPHAFORGE_SCHEDULER=local,AGENTCORE_HARNESS_ARN=$HARNESS_ARN}"
```

**Shortcut without the Gateway (default `ALPHAFORGE_AGENT_TOOLS=inline`).** Any harness works, including one created in the console. On every call the API sends the AlphaForge tools as inline functions, the system prompt from `agents/system_prompt.md`, and `allowedTools` limited to those tools, so the harness's own tools (browser, shell, files) stay unused. When the harness stops with `stopReason=tool_use`, the API runs the tool in-process and returns the result on the same `runtimeSessionId`. In this mode, steps 5 and the `alphaforge-tools` Lambda are optional. Set `AGENTCORE_MODEL_ID` to override the harness model. Set `ALPHAFORGE_AGENT_TOOLS=gateway` to use the Gateway setup above.

## 7. Bedrock Guardrail: responsible-use filter

Create the guardrail in the Bedrock console with the settings in `agents/instructions.md` (denied topics: personalized advice and real transactions; PII filters; prompt-attack filter). Publish a version, then add `BEDROCK_GUARDRAIL_ID=<id>,BEDROCK_GUARDRAIL_VERSION=<n>` to the API Lambda environment as in step 6. The API applies it to the question before the agent runs and to the answer after.

## 8. Frontend: S3 + CloudFront

```bash
cd frontend
VITE_API_BASE=https://$API_ID.execute-api.$REGION.amazonaws.com VITE_AUTH_MODE=cognito npm run build
aws s3api create-bucket --bucket alphaforge-web-$ACCOUNT --region $REGION \
  $( [ "$REGION" != us-east-1 ] && echo --create-bucket-configuration LocationConstraint=$REGION )
aws s3 sync dist/ s3://alphaforge-web-$ACCOUNT/ --delete
cd ..
```

In the CloudFront console, create a distribution:
- **Origin:** the `alphaforge-web-…` bucket with **Origin access control** (keep the bucket private; accept the suggested bucket policy).
- **Default root object:** `index.html`.
- **Error pages:** map 403 and 404 to `/index.html` with status 200 (single-page app routing).

Then allow the CloudFront URL in the API's CORS settings:

```bash
aws apigatewayv2 update-api --api-id $API_ID --cors-configuration \
  '{"AllowOrigins":["https://<dxxxx>.cloudfront.net"],"AllowMethods":["GET","POST","PUT"],"AllowHeaders":["authorization","content-type"]}'
```

## 9. Ingest Lambda and EventBridge Scheduler: live data from reliable sources

One function, `alphaforge-ingest`, pulls data published after the starter window and rebuilds the features the fit score uses. It runs the same code as `python -m alphaforge.ingest.run` locally, against the S3 bundle. Each Monitoring source is one schedule whose input names the source:

| source_id | What it pulls | Writes |
|---|---|---|
| `market_data` | Prices and dividends (Yahoo Finance chart API), USD/CAD and Canadian rates (Bank of Canada Valet), U.S. rates (FRED) | `live_extension.json` |
| `sec_edgar` | 8-K items and periodic reports, XBRL annual fundamentals, Form 4 insider trades (SEC EDGAR) | `signals.json`, `fundamentals.json` |
| `gdelt` | Headlines naming each company from allow-listed outlets (GDELT, else Google News RSS) | `signals.json` |
| `official_rss` | Federal Reserve and Bank of Canada press releases; policy-rate decisions from the rate series | `signals.json` |
| `news_backfill` | Headlines around each asset's largest price moves since 2021 (resumable; run until nothing remains) | `signals.json`, `news_backfill_state.json` |

Every run ends by rebuilding `asset_features.json` and writing `ingest_status.json`. The API reloads the bundle every 15 minutes (`ALPHAFORGE_DATA_TTL_SECONDS`), so new data reaches answers without a redeploy.

```bash
aws iam create-role --role-name alphaforge-ingest-role --assume-role-policy-document file://infra/iam/lambda-trust.json
aws iam put-role-policy --role-name alphaforge-ingest-role --policy-name app \
  --policy-document "$(render infra/iam/ingest-policy.json)"
sleep 10
aws lambda create-function --function-name alphaforge-ingest --runtime python3.12 --architectures arm64 \
  --handler alphaforge.handlers.ingest_lambda.handler --zip-file fileb://dist/alphaforge-lambda.zip \
  --role arn:aws:iam::$ACCOUNT:role/alphaforge-ingest-role --memory-size 1024 --timeout 900 \
  --environment "Variables={$COMMON_ENV,ALPHAFORGE_HTTP_USER_AGENT=AlphaForge-AI datathon <your-email>}"
# Optional: score headline sentiment with Amazon Comprehend instead of the word list
#   add ALPHAFORGE_SENTIMENT=comprehend to the environment.

# First fill (the backfill needs several runs of ~13 minutes; repeat until remaining_moves is 0)
for s in market_data sec_edgar official_rss gdelt news_backfill; do
  aws lambda invoke --function-name alphaforge-ingest --cli-binary-format raw-in-base64-out \
    --payload "{\"source_id\": \"$s\"}" /tmp/ingest-$s.json && cat /tmp/ingest-$s.json | head -c 400; echo
done

aws scheduler create-schedule-group --name alphaforge-ingest
# Role that lets Scheduler invoke the function: alphaforge-scheduler-role (trust scheduler.amazonaws.com,
# permission lambda:InvokeFunction on alphaforge-ingest)
for s in market_data:1d sec_edgar:6h gdelt:6h official_rss:6h; do
  id=${s%%:*}; rate=${s##*:}; expr=$([ "$rate" = 1d ] && echo 'rate(1 day)' || echo "rate(${rate%h} hours)")
  aws scheduler create-schedule --group-name alphaforge-ingest --name alphaforge-ingest-$id \
    --schedule-expression "$expr" --flexible-time-window Mode=OFF \
    --target "{\"Arn\":\"arn:aws:lambda:$REGION:$ACCOUNT:function:alphaforge-ingest\",\"RoleArn\":\"arn:aws:iam::$ACCOUNT:role/alphaforge-scheduler-role\",\"Input\":\"{\\\"source_id\\\":\\\"$id\\\"}\"}"
done
# then set ALPHAFORGE_SCHEDULER=eventbridge on alphaforge-api
```

The Monitoring page changes a source's frequency with `UpdateSchedule` (the target and its input are copied unchanged). Schedule names must be `alphaforge-ingest-<source_id>`. Only users in the Cognito `admin` group can change frequencies, because the setting is global. SEC EDGAR asks for a User-Agent with a contact address; GDELT allows one request every 5 seconds, and the job falls back to Google News RSS when it is rate-limited.

## 10. Check the deployment

- `curl https://$API_ID.execute-api.$REGION.amazonaws.com/health` → `{"status": "ok", ..., "placeholder_data": false}` (`true` if you uploaded the fixture instead of the pipeline output).
- Open the CloudFront URL, sign in, load the Robert preset, ask "Should I put 30% of my portfolio in Tesla?". The answer badge should read **AgentCore agent** and the Trace tab should list `check_constraints`.
- CloudWatch Logs: `/aws/lambda/alphaforge-api` and `/aws/lambda/alphaforge-tools` (each tool call logs `tool=... status=...`).

## 11. Clean up

Delete in reverse order: CloudFront distribution, web bucket, harness, gateway target and gateway, Lambda functions, API, user pool, DynamoDB tables, data bucket, IAM roles, guardrail.

## Services deliberately not used

- **Kinesis:** the signal volume is small (a few thousand items), so Scheduler and Lambda are enough.
- **SageMaker training:** the brief does not ask for return prediction, and we avoid that framing.
- **OpenSearch Serverless:** it has a minimum cost; if a filings knowledge base is added, S3 Vectors is the cheaper store.
- **EC2 or containers:** every component is request-driven and short-lived.
