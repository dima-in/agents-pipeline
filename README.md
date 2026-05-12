# agents-pipeline

`agents-pipeline` is a local-first multi-phase agent workflow that can run from its own engine repository while analyzing or modifying another project directory.

## Architecture

The pipeline has two separate roots:

- `engine_root`: the `agents-pipeline` repository itself
- `target_workspace`: the project being analyzed or changed

Engine-owned assets stay under `engine_root`:

- `workflow/config.yaml`
- `workflow/*.py`
- `.openclaw/agents/**`
- `manage_agents.py`
- `run.bat`
- tests and docs

Workspace-owned operations stay under `target_workspace`:

- repository context collection
- retrieval-loop file access
- `git status`, `git log`
- implementation branch, merge, and rollback operations

Logs stay under `engine_root/.openclaw/logs/<project_id>/run_*` even when the target workspace points somewhere else.

Project-specific state stays under:

- `.agents-pipeline/projects/<project_id>/context`
- `.agents-pipeline/projects/<project_id>/memory`
- `.agents-pipeline/projects/<project_id>/logs`
- `.agents-pipeline/projects/<project_id>/summaries`
- `.agents-pipeline/projects/<project_id>/settings.yaml`

## Executors

There are two execution modes in the codebase:

- `openclaw`: the legacy runtime path that shells out to the OpenClaw CLI
- `direct_api`: the current independent runtime path that calls OpenRouter chat completions directly

`direct_api` exists because workflow execution should not depend on OpenClaw runtime stability or OpenClaw-local agent execution semantics. The pipeline still keeps OpenClaw-compatible prompts, agent directories, and registration helpers, but the orchestration path now owns prompt assembly, local repository context, retrieval, report persistence, and runtime accounting itself.

## direct_api Flow

`WorkflowOrchestrator` is the control plane for:

- phase sequencing
- runtime resolution
- prompt assembly
- target workspace selection
- report persistence
- usage and cost tracking

For `direct_api` research agents the orchestrator:

1. loads the agent prompt from `engine_root`
2. builds a role-specific repository context profile
3. injects previous-agent handoff summaries when needed
4. optionally enables a bounded retrieval loop
5. sends the final request to OpenRouter
6. saves normalized JSON and Markdown reports under `.openclaw/logs`

## Retrieval Loop

The retrieval loop is available only for research agents that actually need deeper local inspection:

- `project-analyst`
- `tech-analyst`

Supported tools:

- `read_files`
- `search_text`
- `list_files`

Safety rules:

- paths are resolved relative to `target_workspace`
- escaping `target_workspace` is blocked
- retrieval diagnostics are logged per run

The retrieval loop is intentionally disabled for the other research roles so their context stays constrained to the intended abstraction level.

## Research Context Profiles

Each active research role gets a different context shape.

`project-analyst`
- full repo overview
- target repo git status/log
- target README
- workflow config
- orchestrator outline
- runtime excerpt
- tests list
- direct access to retrieval

`competitor-analyst`
- compressed project summary
- target README
- workflow architecture summary
- no full orchestrator dump
- no raw implementation detail dump

`market-analyst`
- project positioning
- workflow goals
- target users and use cases
- no code context

`tech-analyst`
- execution architecture
- orchestrator outline
- direct_api sections
- retrieval-loop sections
- runtime excerpt
- relevant tests

`innovation-scout`
- compressed project summary
- architecture summary
- current constraints
- roadmap context
- no raw repo dump

`product-manager`
- summaries from all previous research agents
- no raw repository context
- no retrieval loop

## Research Handoffs

After every successful research agent, the orchestrator writes a compressed deterministic handoff summary with this structure:

```text
agent: <agent-name>
findings:
- ...
risks:
- ...
decisions:
- ...
recommended_next_tasks:
- ...
```

Rules:

- capped at 2000 characters
- deterministic formatting
- reusable by downstream agents
- `product-manager` consumes these summaries instead of a raw repo dump

## Cost Guardrails

Set a phase budget in `workflow/config.yaml`:

```yaml
workflow:
  max_phase_cost_usd: 2.50
```

Behavior:

- cost is accumulated from saved agent reports
- phase and run totals are logged continuously
- once the current phase total exceeds the limit, the phase stops before scheduling more agents

## Running Against Another Workspace

Default behavior:

- if `--workspace` is passed, it becomes `target_workspace`
- if `--project-id` is passed, it overrides automatic project identity detection
- if omitted, the current launch directory becomes `target_workspace`
- if launched from `engine_root`, the fallback target is `project.workspace` from `workflow/config.yaml`

Examples:

Run against the current directory:

```powershell
cd /d D:\SomeProject
D:\agentic-dev-loop\agents-pipeline\run.bat --phase research --mode interactive
```

Run against an explicit directory:

```powershell
D:\agentic-dev-loop\agents-pipeline\run.bat --phase research --workspace D:\SomeProject
```

## Reports

Each run writes:

- `.openclaw/logs/<project_id>/run_*/agents/<phase>/<agent>.json`
- `.openclaw/logs/<project_id>/run_*/agents/<phase>/<agent>.md`
- `.openclaw/logs/<project_id>/run_*/<phase>-summary.md`
- `.openclaw/logs/<project_id>/run_*/run_summary.json`

Research reports also carry:

- `context_profile`
- `repository_context_chars`
- `handoff_summary_chars`
- `retrieval_enabled`
- `retrieval_rounds`
- `target_workspace`
- `project_id`

## Setup

```powershell
git clone <YOUR_GITHUB_URL>
cd agents-pipeline
Copy-Item .env.example .env
.\install.bat
.\run.bat python manage_agents.py bootstrap
```

If you still use OpenClaw registration for compatibility, you can also run:

```powershell
.\run.bat python manage_agents.py register-all
```

## Tests

Run the full suite with:

```powershell
.\run.bat python -m pytest
```
