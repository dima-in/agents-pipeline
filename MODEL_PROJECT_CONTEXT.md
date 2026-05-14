# Project Context For LLM

## Identity

- Project: `agents-pipeline`
- Project ID: `github.com-dima-in-agents-pipeline`
- Git remote: `https://github.com/dima-in/agents-pipeline.git`
- Primary workspace: `D:\agentic-dev-loop\agents-pipeline`
- Current mode: local-first multi-phase agent pipeline

## What This Project Does

`agents-pipeline` is an orchestration engine for running multi-phase AI agent workflows against either:

- itself (`engine_root == target_workspace`)
- an external repository (`target_workspace != engine_root`)

It supports:

- research phase
- implementation phase
- deployment phase

The system separates:

- `engine_root`: the pipeline repository and orchestration code
- `target_workspace`: the repository being analyzed or modified

This separation is critical. Repository context, retrieval, git operations, write operations, and scope policy must stay bound to `target_workspace`.

## Runtime Model

- Main executor: `direct_api`
- Current provider path: OpenRouter-compatible chat completions
- Legacy compatibility path: `openclaw` subprocess execution

Important architectural point:

- prompts and agent definitions still live under `.openclaw/agents/**`
- execution logic, retrieval, validation, context assembly, write controls, and report persistence are owned by `workflow/orchestrator.py`

## Main Entry Points

- `run.bat`
- `run.sh`
- `start.py`
- `run_launcher.py`
- `manage_agents.py`

## Core Code Areas

### Workflow engine

- `workflow/orchestrator.py`
  - central control plane
  - phase sequencing
  - prompt assembly
  - retrieval tool loop
  - direct API execution
  - planner validation
  - scope watchdog
  - write controls
  - report persistence

- `workflow/runtime.py`
  - runtime/provider resolution

- `workflow/logger.py`
  - run and agent logging

- `workflow/config.yaml`
  - workflow configuration
  - agent list
  - runtime defaults
  - console language
  - implementation scope defaults and policy

- `workflow/pricing.yaml`
  - token pricing metadata

### Repo map and context tooling

- `tools/repo_map.py`
  - generates repository map
  - validates planner/developer paths
  - compares before/after repo maps

### Agent prompts and configs

- `.openclaw/agents/research/**`
- `.openclaw/agents/implementation/**`
- `.openclaw/agents/deployment/**`

Important implementation agents:

- `architect`
- `implementation-planner`
- `developer`
- `qa`
- `template-validator`

## Phase Model

### Research

Goal:

- analyze repository
- understand product/technical context
- produce compact handoff summaries

Important agents:

- `project-analyst`
- `competitor-analyst`
- `market-analyst`
- `tech-analyst`
- `innovation-scout`
- `product-manager`

Research output is persisted and later reused by implementation.

### Implementation

Goal:

- convert research and architect output into a constrained implementation task
- validate it against repo reality and scope policy
- make actual file changes or fail clearly

Important agents:

- `architect`
- `implementation-planner`
- `developer`
- `qa`
- `template-validator`

### Deployment

Goal:

- launch/readiness analysis after implementation

## Current Implementation Scope Defaults

Configured default scope:

`Implement backend-only MVP provider performance monitoring and smart routing foundation. No marketplace, no Stripe changes, no frontend changes except API client stubs if required.`

Hard behavior for implementation agents:

- prefer small backend-first changes
- do not implement marketplace
- do not change Stripe/billing/subscription logic
- do not perform broad frontend changes
- do not do unrelated refactors
- stop and report if scope must widen

## Implementation Scope Policy

The implementation watchdog enforces a policy from `workflow.config.yaml`.

Policy includes:

- `allowed_paths`
- `forbidden_paths`
- `forbidden_keywords`
- `max_changed_files`
- `max_diff_lines`

The watchdog checks:

1. planner-selected task contract before developer runs
2. write targets during `write_file` / `apply_patch`
3. real git diff after developer runs

If violated, the phase is blocked with `scope_violation`.

## Planner Contract

`implementation-planner` is expected to return structured backlog items with fields such as:

- `id`
- `title`
- `priority`
- `existing_paths`
- `new_directories`
- `new_files`
- `allowed_paths`
- `forbidden_paths`
- `required_test_paths`
- `acceptance_criteria`
- `reason_each_path_is_needed`

Important validation rules:

- every `allowed_path` must also appear in:
  - `existing_paths`
  - or `new_files`
  - or `new_directories`
- new files must live under real existing directories or declared new directories
- planner must use repo-map-backed real paths
- invalid planner output produces structured feedback
- retry prompt automatically includes prior validation feedback

## Retrieval And Write Model

### Research retrieval

Allowed for selected research agents only:

- `read_files`
- `search_text`
- `list_files`

Boundaries:

- confined to `target_workspace`
- path escape is blocked

### Implementation retrieval and writes

Implementation agents may receive controlled tool access.

Developer can use:

- `read_file`
- `read_files`
- `list_files`
- `search_text`
- `write_file`
- `apply_patch`

Restrictions:

- writes allowed only for implementation `developer`
- writes must stay inside `target_workspace`
- writes must satisfy selected task contract
- writes must satisfy implementation scope policy
- if no actual diff exists after developer, result becomes `no_changes`

QA behavior:

- must inspect actual git diff
- fails with `no_changes` if developer produced no real file change

## Research Handoff Model

Implementation depends on successful research summaries for the same `project_id`.

Architect receives a bundle that includes:

- latest successful `product-manager` summary
- `project-analyst` summary
- `tech-analyst` summary
- target docs/dependency excerpts
- target tree
- selected implementation scope

If implementation context is missing, preflight fails before architect.

## Project-Scoped State

Per-project state lives under:

- `.agents-pipeline/projects/<project_id>/settings.yaml`
- `.agents-pipeline/projects/<project_id>/context/repo_map.json`
- `.agents-pipeline/projects/<project_id>/memory/`
- `.agents-pipeline/projects/<project_id>/logs/`
- `.agents-pipeline/projects/<project_id>/summaries/`

Run-scoped logs and reports live under:

- `.openclaw/logs/<project_id>/run_*/...`

Feedback now lives under:

- `.openclaw/feedback/<project_id>/<run_id>/attempt_<n>/<agent>.md`
- `.openclaw/feedback/<project_id>/latest/<agent>.md`

Each feedback file includes metadata such as:

- `project_id`
- `run_id`
- `attempt`
- `phase`
- `agent`
- `target_workspace`
- `repo_map_target_workspace`
- `context_mode`
- `created_at`

## Important Consistency Checks

The project now guards against context contamination and workspace mismatch.

Key checks:

- `context_mode`
  - `engine_self_analysis`
  - `external_project_analysis`
- `repo_map_target_workspace` must match `target_workspace`
- research handoff must exist before implementation
- planner paths must match repo map reality
- developer writes must match allowed scope

If `repo_map_target_workspace != target_workspace`, implementation is blocked before planner/developer.

## Current Console Behavior

- `workflow.console_language` exists
- default console language: `ru`
- implementation backlog selection is localized to Russian by default
- task IDs, priorities, and paths stay unchanged

## Key Files For Model Attention

If a model needs to understand or modify the system, start here:

1. `README.md`
2. `workflow/config.yaml`
3. `workflow/orchestrator.py`
4. `workflow/runtime.py`
5. `workflow/logger.py`
6. `tools/repo_map.py`
7. `.openclaw/agents/implementation/implementation-planner/prompt.md`
8. `tests/test_workflow.py`
9. `tests/test_agent_reports.py`
10. `tests/test_repo_map.py`

## Current Risks / Caveats

- `workflow/orchestrator.py` is very large and holds multiple responsibilities
- `.openclaw` is still used as prompt/layout/log storage even though execution has moved to `direct_api`
- repo map may include noisy transient files if cleanup/exclusion rules are incomplete
- implementation safety depends heavily on structured planner output quality

## Useful Mental Model

This project is best understood as:

- a workflow engine
- with agent-role prompts
- plus a repository-aware context builder
- plus a constrained file-write sandbox
- plus phase-scoped and project-scoped validation layers

It is not just a prompt runner. The important logic is in orchestration, validation, scope control, repo mapping, and persistence.
