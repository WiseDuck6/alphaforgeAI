# Agent design

Classic Amazon Bedrock Agents is in maintenance mode and closed to new customers, so AlphaForge uses **Amazon Bedrock AgentCore**:

| Component | Service | Role (why it exists) |
|---|---|---|
| **Advisor agent** | AgentCore **Harness** (managed agent loop, no orchestration code) | Understands the question, decides which tools to call, writes the answer in the explainability contract. |
| **Tools** | AgentCore **Gateway** with one **Lambda target** (`alphaforge.handlers.gateway_tools_lambda.handler`) | Exposes the deterministic engines as MCP tools: profile features, asset features, news and filings, price-move explanations, market regime, fit scores, governance, radar, suggestions and stress. All numbers come from here. |
| **Reasoning summary** | The model's `reasoning` list (or, if missing, one step per tool call) | Shows the observable steps behind an answer (plan, gather, evaluate, challenge, answer) with citations into the evidence list. Its numbers are verified like the answer's. Local mode builds the same structure in code (`backend/alphaforge/reasoning.py`). |
| **Data freshness** | Lambda `alphaforge-ingest` on EventBridge Scheduler | Pulls prices, rates, SEC filings, fundamentals, central-bank releases and allow-listed news after the starter window, then rebuilds the features the tools read. |
| **Governance** | Code in the API Lambda + `check_constraints` tool | For allocation requests, the governance check runs in code whether or not the model calls it, and is attached to the answer. |
| **Responsible-use guardrail** | Amazon Bedrock **Guardrails** via `ApplyGuardrail` | Screens the question and the answer for personalized advice, real-trade requests and PII. |
| **Explainability check** | Code in the API Lambda (`verify_numbers`) | Every percentage, decimal and x/100 score in the answer must match a tool output; unmatched figures are listed and confidence drops to low. |
| **Trace** | Gateway Lambda writes each call to DynamoDB `agent_sessions` | The user sees the tool calls (inputs, outputs) behind the answer, not the model's internal reasoning. |

Files:
- `system_prompt.md`: the advisor's instructions (harness `systemPrompt`).
- `gateway_tools.json`: inline tool schema for the Gateway Lambda target. Regenerate with `python -m alphaforge.agent_tools > ../agents/gateway_tools.json` after changing tools.
- `harness_config.json`: template for `create-harness`.

## How a question flows

1. Browser `POST /ask` → API Lambda checks the Cognito user, stores a job, invokes itself asynchronously and returns `job_id` (API Gateway allows 30 seconds; an agent may need more).
2. Job: `ApplyGuardrail` on the question → governance check in code if it is an allocation request → random `context_key` bound to the user in `agent_sessions` (15-minute TTL) → `InvokeHarness`.
3. The harness calls Gateway tools with the `context_key`. The Gateway Lambda resolves the key to the user, loads the profile, runs the engine, logs the call.
4. API Lambda reads the logged calls, parses the JSON answer, runs `ApplyGuardrail` on the answer and `verify_numbers`, stores the result in `agent_traces`.
5. Browser polls `GET /ask/{job_id}` and renders Answer / Evidence / Assumptions / Limits / Trace.

The model never sees a user id, so it cannot read another user's profile; a forged or expired key returns an error.

## Guardrail settings (create in the Bedrock console → Guardrails)

- **Denied topics**
  - *Personalized investment advice*: "Telling a specific person to buy, sell or hold a specific security with real money, or guaranteeing returns."
  - *Real transactions*: "Placing, routing or confirming real trades or money transfers."
- **Sensitive information filters**: block account numbers, SIN/SSN and card numbers; anonymize email and phone.
- **Content filters**: default strength for hate, insults, sexual, violence, misconduct; prompt attack on input.
- **Blocked message**: "AlphaForge is an educational simulator and can't help with that. Try asking how an asset fits your fictional profile."
- Create a version and put its id and version number in the API Lambda environment (`BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION`).

Test the denied topics in the console before relying on them: a too-broad definition will block legitimate questions such as "Should I put 30% in Tesla?", which the app is meant to answer with a challenge rather than a refusal.
