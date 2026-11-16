# AlphaForge AI

**Team 18:** Kevin Xu, Jeremy Vong, Sheng Hege, Song Ning Lan

An explainable investment-analysis simulator for the PolyFinances Datathon 2026. It compares assets with fictional investor profiles, analyzes portfolio risk, checks allocation constraints, and answers questions with evidence, assumptions and limitations.

**Simulation only:** no real transactions or personalized financial advice.

## Requirements

| Dependency | Use |
|---|---|
| Python **3.12+** | Backend and data processing; commands below use Python 3.12. |
| Node.js **22.13+**, preferably a supported LTS release, with npm | React frontend. Node 20.17 is too old. |
| AWS CLI v2, current version | AWS login and agent access; not needed for local mode. |
| `boto3[crt]>=1.44` | Python AWS integration; installed below when connecting the agent. |
| `pytest>=8` | Optional tests. |

The included CSVs are sufficient to start. Re-downloading them requires additional dependencies; see [starter data notes](alphaforge_starter/README.md).

## Run locally (Windows PowerShell)

Run the commands below from the repository root. Keep `alphaforge_starter` beside `alphaforge-ai`.

### 1. First-time backend setup

```powershell
cd .\alphaforge-ai\backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe ..\data_pipeline\build_bundle.py
```

This builds `data_pipeline/out` from the supplied CSVs without AWS or network access. You do not need to activate the virtual environment.

### 2. Start the backend (terminal 1)

```powershell
cd .\alphaforge-ai\backend
$env:ALPHAFORGE_DATA_SOURCE = "dir:$((Resolve-Path ..\data_pipeline\out).Path)"
$env:ALPHAFORGE_STORAGE = "local"
$env:ALPHAFORGE_AUTH_MODE = "header"
Remove-Item Env:AGENTCORE_HARNESS_ARN -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m alphaforge.devserver
```

The API runs at **http://127.0.0.1:8000**. Local Ask uses a rules-based engine, not an LLM; try `Compare SPY and ESGV`. Without `ALPHAFORGE_DATA_SOURCE`, it uses placeholder metrics.

### 3. Start the frontend (terminal 2)

```powershell
cd .\alphaforge-ai\frontend
npm.cmd ci --include=optional
npm.cmd run dev
```

Open **[http://127.0.0.1:5173](http://127.0.0.1:5173)** and select Robert, Amira or Mateo. Leave both terminals running; **Ctrl+C** stops a server.

On later runs, skip environment creation, data generation and dependency installation unless something changed. Reapply the backend environment variables in each new terminal.

## Connect the AWS agent

The app can stay local while AgentCore runs the model on AWS. You do not need to deploy Lambda, S3, DynamoDB or the frontend for this mode. AWS usage can incur charges.

### 1. Authenticate

Install AWS CLI v2 if needed, then reopen PowerShell:

```powershell
winget install --exact --id Amazon.AWSCLI --source winget
```

Use your workshop's temporary-credential instructions, your organization's SSO login, or browser login where supported:

```powershell
aws login
aws sts get-caller-identity
```

For a named profile, set `$env:AWS_PROFILE = "your-profile-name"` in the backend terminal. Never put credentials in source files or frontend `VITE_*` variables.

### 2. Restart the backend in agent mode

Stop the backend with **Ctrl+C**, then run:

```powershell
cd .\alphaforge-ai\backend

# First time, or when updating the AWS SDK
.\.venv\Scripts\python.exe -m pip install --upgrade "boto3[crt]>=1.44"

$env:AWS_REGION = "us-east-1"
$env:AWS_DEFAULT_REGION = "us-east-1"
$env:AGENTCORE_HARNESS_ARN = "arn:aws:bedrock-agentcore:<region>:<account-id>:harness/<harness-id>"
$env:ALPHAFORGE_AGENT_TOOLS = "inline"
$env:ALPHAFORGE_DATA_SOURCE = "dir:$((Resolve-Path ..\data_pipeline\out).Path)"
$env:ALPHAFORGE_STORAGE = "local"
$env:ALPHAFORGE_AUTH_MODE = "header"
Remove-Item Env:AGENTCORE_MODEL_ID -ErrorAction SilentlyContinue

.\.venv\Scripts\python.exe -m alphaforge.devserver
```

Replace the placeholder with the ARN of your own AgentCore harness. The frontend can stay running. Refresh **[/ask](http://127.0.0.1:5173/ask)**; a successful response should show **AgentCore agent**.

The backend supplies the prompt from `alphaforge-ai/agents/system_prompt.md` and runs the portfolio tools locally. The separately generated `AlphaForgeAgent` project is not used by this connection; deleting its folder would not delete its AWS resources.

### AWS access requirements

- The local AWS identity needs permission to invoke the harness and its required runtime operation (`InvokeHarness` / `InvokeAgentRuntime`).
- The harness execution role needs model invocation permissions, including streaming access, for the selected model/inference profile.
- Third-party models may require account-level activation. During setup, the harness used `global.anthropic.claude-opus-5` and failed because its Marketplace agreement was not active. An authorized administrator or workshop organizer must enable the model; normal calls afterward do not need subscription permissions.

See [AWS model activation](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) and [harness IAM requirements](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html).

## Configuration essentials

| Variable | Purpose |
|---|---|
| `ALPHAFORGE_DATA_SOURCE` | `fixture`, `dir:<absolute path>`, or `s3://bucket/prefix`. |
| `AGENTCORE_HARNESS_ARN` | Set for the AWS agent; unset for local Ask. |
| `AGENTCORE_MODEL_ID` | Optional model/inference-profile override; otherwise use the harness model. |
| `ALPHAFORGE_AGENT_TOOLS` | `inline` runs tools in the backend; `gateway` requires a configured AgentCore Gateway. |
| `AWS_PROFILE` | Optional named credentials profile. |
| `VITE_API_BASE` | API base URL for a hosted frontend. Local development uses Vite's `/api` proxy. |

PowerShell `$env:` settings last for that terminal only. Restart Python after changing backend settings; it does not automatically read `.env`. Rebuild the production frontend after changing `VITE_*` values. `header` authentication is for local development only.

## Tests and builds

**Backend and data tests** — from `alphaforge-ai/backend`:

```powershell
.\.venv\Scripts\python.exe -m pip install "pytest>=8"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest ..\data_pipeline\tests
```

These tests do not validate live AWS access.

**Frontend build** — from `alphaforge-ai/frontend`:

```powershell
npm.cmd run build
```

Output: `frontend/dist`. For hosting, configure `VITE_API_BASE` and authentication before building. Static hosting does not provide the development API proxy. `npm.cmd run preview` previews the build only. The frontend's Cognito sign-in flow still needs implementation for protected production API calls.

**Lambda package** — from `alphaforge-ai` in Linux/WSL, with Bash, `zip` and Python/pip or `uv`:

```bash
bash scripts/package_lambda.sh
```

Output: `dist/alphaforge-lambda.zip`. Packaging does not deploy AWS resources. Follow the [AWS deployment guide](alphaforge-ai/docs/aws_deployment.md) for deployment, IAM templates and hosting.

## Troubleshooting

| Problem | Fix |
|---|---|
| Python 3.12 not found | Run `winget install --exact --id Python.Python.3.12 --source winget`, reopen PowerShell, and check `py -0p`. |
| Node is too old / Rolldown native binding missing | Update Node with `winget install --exact --id OpenJS.NodeJS.LTS --source winget`, reopen PowerShell, then run `npm.cmd ci --include=optional`. Keep the lockfile. |
| AWS CLI installed but `aws` not found | Reopen the terminal/IDE. For the standard install, use `$env:Path = "C:\Program Files\Amazon\AWSCLIV2;" + $env:Path` in the current terminal. |
| Ask says “the agent could not answer” | Read the Python terminal traceback. Marketplace `AccessDeniedException` requires model activation/permissions; expired credentials require renewed login. |
| Ask shows “Local reasoning engine” | Set the harness ARN in the terminal that starts Python, then restart it. |
| Placeholder data / `Resolve-Path` fails | Build the data bundle first and set its absolute path. |
| Frontend cannot reach API | Keep the backend on port 8000 and the frontend running in separate terminals. |

## Further documentation

- [AWS deployment guide](alphaforge-ai/docs/aws_deployment.md): cloud configuration and optional live ingestion.
- [Data contract](alphaforge-ai/docs/data_contract.md): transformations, sources and limitations.
- [Technical documentation](TECHNICAL_DOCUMENTATION.pdf): architecture and methods.
- [One-pager](ONE_PAGER.pdf): project summary.
