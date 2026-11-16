# Deliverables tracker

The brief asks for four deliverables that tell one story. Status as of 2026-11-14.

| # | Deliverable | What the brief asks | Status |
|---|---|---|---|
| 1 | **Prototype + code** | A complete, demonstrable use case with an accessible explanation of the result (sources, assumptions, key factors, limits). Repository README with dependencies, data, configuration and reproduction. | Runs locally end to end (dashboard, radar, scores, suggestions, governance, Ask with trace). Still to do: data pipeline, Cognito sign-in screen, AWS deployment, monitoring ingest. |
| 2 | **Video (~5 min)** | Problem, role of the agents, data and tools, result, end-to-end explanation. Two contrasting investor situations. | Script outline below. Record after the real data and the AWS agent are connected. |
| 3 | **Technical documentation** | Architecture and agent roles/interactions and controls; methods and why; sources, assets, processing, assumptions; explainability mechanism with an example traced to data; validation, limits, improvements. | Pieces exist: `agents/instructions.md`, `docs/data_contract.md`, `docs/aws_deployment.md`, test suite. Assemble into one document (outline below). |
| 4 | **One-pager** | At most one page: problem, agentic approach, originality, data sources, explainability, user value. | Outline below. |

## Scoring reminders (from the presentation)

- Solution 20%: relevance, innovation, creativity and the prototype.
- Agentic design and explainability 20%.
- Data 20%: rigour, validation and visualization.
- UX/UI 10%.
- AWS 15%. This is qualifying: without real AWS integration the project is not eligible.
- Pitch 15%.

Historical portfolio performance is explicitly not the goal.

## Video script outline (about 5 minutes)

1. **0:00–0:30, the problem.** Investors have too much data and too little explanation. AlphaForge connects market data, events and personal constraints, and shows its evidence.
2. **0:30–1:15, architecture.** Show the diagram: deterministic engines, the AgentCore advisor, the Gateway tools, guardrails, figure verification, and the AWS services with one line each.
3. **1:15–2:30, Robert (contrast A).** Dashboard → "Should I put 30% in Tesla?". The answer shows the conflict, the numbers, the largest compatible position and fund alternatives. Then open the Trace tab and the "figures verified" badge.
4. **2:30–3:30, Amira (contrast B).** High risk but concentrated. The radar shows the gaps, the suggestions carry what-if impacts, and crypto is capped at 10%. "High risk does not mean anything goes."
5. **3:30–4:15, Mateo.** The ESG screen excludes ENB, CNQ and XOM (shown, not hidden) and points to ESGV. Note that sector flags are not ESG ratings.
6. **4:15–4:45, monitoring.** Change a source's frequency, show the EventBridge schedule update and an alert on a holding.
7. **4:45–5:00, limits and next steps.** Simulation only, survivorship bias, placeholder vs pipeline data.

## Technical documentation outline

1. Problem and users (the personas, and the decision being supported)
2. Architecture (diagram; each AWS service and why it is there; services deliberately not used)
3. Agents and controls (advisor harness, tools, governance in code, guardrails, number verification, trace)
4. Methods (profile-fit score formula and weights; radar axes and targets; suggestion rules; stress replay; why deterministic engines rather than an LLM for numbers)
5. Data (sources, dates, licences; processing rules from `docs/data_contract.md`; assumptions table)
6. Explainability, with one worked example (Robert/TSLA) traced from answer → tool call → metric → CSV row
7. Validation (unit tests per persona, figure verification, data-quality report, sanity checks)
8. Limits and improvements

## One-pager outline

- **Problem:** one sentence.
- **Approach:** deterministic engines plus an AgentCore advisor, with governance and verification in code.
- **What is original:** the profile-fit score and radar gap analysis with what-if impacts; the agent cannot state a number that no tool produced; it challenges requests instead of agreeing.
- **Data:** starter dataset, plus the sources you add (ESG evidence, filings, events), with dates.
- **Explainability:** evidence, assumptions, limits and trace on every answer.
- **User value:** one line per persona.
