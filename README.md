# agents-pipeline

`agents-pipeline` is a local-first, multi-phase agent workflow that autonomously implements backlog tasks INTO a target repository. It is **universal**: the engine names zero projects; every fact about the target (language, persistence model, schema, join paths, layout, conventions) is detected from the target repo itself or filled in by a profiler agent. The same engine onboards a SQLAlchemy/Alembic service and a hand-written raw-MySQL FastAPI app without code changes.

## Quick start

```powershell
git clone <YOUR_GITHUB_URL>
cd agents-pipeline
Copy-Item .env.example .env   # put OPENROUTER_API_KEY here (start.py loads .env for missing vars)
.\install.bat

# research once per project (builds the backlog), then implement task by task:
venv\Scripts\python.exe start.py --phase research --workspace D:\SomeProject --goal "..."
venv\Scripts\python.exe start.py --phase implementation --workspace D:\SomeProject --mode auto --task-id TASK-001
```

A green implementation run costs around $0.10–0.35 (OpenRouter, mixed sonnet/gpt models).

## Architecture: two roots

- `engine_root`: this repository — config, prompts, orchestrator, logs, per-project state
- `target_workspace`: the project being analyzed or changed (`--workspace`)

The engine never edits itself while targeting another repo; the target repo never receives engine files. Logs stay under `engine_root/.openclaw/logs/<project_id>/run_*`. Per-project state persists under `.agents-pipeline/projects/<project_id>/`:

- `settings.yaml` — user goal, scope policy (allowed/forbidden paths), completed tasks
- `context/architecture_profile.json` — the Architecture Profile (see below)
- `context/repo_map.json` — repo map snapshot
- `codex.md` / `resume.md` — durable project notes + resume checkpoint (synced via git)
- `state/implementation_backlog.json` — the canonical task backlog

`project_id` derives from the target's git remote (e.g. `github.com-user-repo`).

## Phases and agents

**Research** (once per goal): project-analyst, competitor/market/tech-analyst, innovation-scout, product-manager. Produces requirements and a backlog. Each role gets a tailored context profile and a deterministic ≤2000-char handoff summary for the next agent.

**Implementation** (per task, up to 3 attempts with git rollback between):

1. `codebase-profiler` — fills `unknown` fields of the Architecture Profile by reading the data layer (skipped when deterministic detectors verified everything)
2. `architect` — technical plan + persisted `## Target architecture` end-state
3. `implementation-planner` — backlog (skipped when the canonical backlog exists)
4. `task-designer` — turns one backlog item into a machine-executable contract (`must_contain`, `must_import`, `must_test`, `forbidden`, ...), validated deterministically; on validation failure it regenerates with the validation feedback (2 retries)
5. `developer` — in `multi_developer_json` mode the work is routed to `code-developer` / `infra-developer` / `test-developer` by file type; edit agents use a JSON tool protocol (`read_file(s)`, `search_text`, `list_files`, `write_file`, `apply_patch`, `tool_batch`)
6. deterministic developer checks — `py_compile`, pytest on the contract's test file, `must_contain` symbol gate
7. `qa` — verdict against the contract (mandatory first line `Вердикт QA: ПРИНЯТО|ОТКЛОНЕНО`)
8. `template-validator` — structure check (explicit `Статус: PASSED|FAILED` line)

**Deployment readiness** (optional): production-readiness-checker, launch-strategist.

## Grounding (why agents don't hallucinate the stack)

- **Architecture Profile** — confidence-tagged facts `{verified|inferred|unknown}` about the CURRENT codebase: language, persistence (engine, access style, sync/async, session pattern), migrations, layout, tests root. Deterministic detectors fill what they can (`verified`); the profiler agent fills gaps (`inferred`); verified facts are never overridden. The architect's intended END-STATE is captured and carried across tasks as the Target architecture.
- **DB schema ground truth** — for raw-SQL repos, `CREATE TABLE` statements are parsed into exact table/column lists **and FOREIGN KEY join paths**. Planners see which tables are linked and which are NOT ("the ONLY declared join paths"), so a metric that needs an impossible join gets narrowed by the architect itself instead of shipping invented columns.
- **Sync/async guardrail** — AST detection of the real DB concurrency + injected-session pattern; contracts demanding async DB on a sync stack are rejected at design time.
- **Evidence-checked obsolescence** — when the task-designer claims a task is "already implemented", the engine extracts checkable tokens (URLs, camelCase/snake_case identifiers) from the acceptance criteria and greps the task's files; a false claim gets an explicit disproof in the retry feedback.

## Deterministic gates (fail-closed)

- `must_contain` gate: def/class symbols matched by AST name, decorators (e.g. `@app.get("/api/...")`) by quote/whitespace-normalized substring against the FULL file — immune to truncated read excerpts. Runs both post-hoc and **within-turn** (a developer cannot finalize while required symbols are missing).
- Write-reserved turns: reads can spend the retrieval budget, but edit agents always keep extra turns where read requests are bounced with a force-write instruction — reads can no longer starve the write.
- Truncated-write repair: a tool-request-looking final answer that failed to parse (e.g. a whole-file `write_file` cut by max_tokens) is never accepted; the loop demands small `apply_patch` hunks instead.
- QA/validator verdicts are **fail-closed**: an unrecognized verdict phrasing is a rejection by default; a validator report full of `**FAILED**` sections fails even without a status line. Import placement (module-level vs function-local) is non-blocking style, not a violation.
- Scope policy: per-project `forbidden_paths` (checked first, always win) + per-task `allowed_paths` (the only editable files). Sensitive files (billing/auth/payment...) are auto-derived from the repo map by keyword. Test-less tasks (frontend/docs/config) don't demand a test contract.

## Console observability (Russian, compact by default)

`logging.console_verbosity: compact` shows only the operator story; everything else goes to the run log file:

```text
Агент запущен: task-designer
+-- Передача: Планировщик -> Конструктор задачи
| Должен: превратить одну задачу в строгий контракт
| По задаче: TASK-003 — frontend-only
| Суть: Add AdminAnalytics component API client stub -> frontend/src/lib/api.js
| Критерий: api.js exports fetchCustomerAnalytics, ...
+--
Агент завершен: task-designer [успех]
Стоимость: агент $0.0317 | фаза $0.1126 | прогон $0.1126
+-- Готово: Конструктор задачи
| Сделал: контракт для TASK-003: 8 требований
| Вывод: <первые строки ответа агента>
+--
```

Failure statuses are translated (`[QA отклонил]`, `[валидатор отклонил]`, `[без изменений]`, ...). Cost lines are first-class and never hidden. Set `verbose` to restore the full stream. Tip for Windows consoles: `setx PYTHONUTF8 1`.

## Executors

- `direct_api` (current): calls OpenRouter chat completions directly; owns prompt assembly, retrieval, gates, reports, cost accounting. Requires `OPENROUTER_API_KEY` (env or `.env`).
- `openclaw` (legacy): shells out to the OpenClaw CLI.

Note: OpenAI *codex* models do not work as edit agents in the JSON-in-content protocol (they loop retrieval and never write); use chat models for developer/qa.

## Cost guardrails

```yaml
workflow:
  max_phase_cost_usd: 2.50
```

Per-agent/phase/run cost is printed after every agent and accumulated from saved reports; the phase stops before scheduling more agents once the limit is exceeded.

## CLI reference (most used)

```text
--phase research|implementation|deployment|full
--workspace <dir>          target repo (else: launch dir)
--mode auto                no blocking prompts; stops after the selected task
--goal "..."               business-level objective for research/implementation
--task-id TASK-001         pick a backlog item (or --next-task)
--rerun-completed          allow re-running a task recorded as completed
--list-tasks               print the persisted backlog and exit
--mark-selected-complete   mark the selected task complete without running agents
--from-agent X / --retry-agent X / --reuse-architect   partial reruns
--skip-git                 disable branch/merge/rollback
```

## Reports

Each run writes JSON+Markdown per agent, a phase summary, `run_summary.json` (tokens, estimated cost) and `human_report.md` under `.openclaw/logs/<project_id>/run_*/`.

## Known limitations

- The engine does **not commit** the delivered work in the target repo: a successful run leaves the changes in the working tree (the feature-branch merge is a no-op branch dance). Commit manually before the next run, or the next rollback stash may eat the result.
- Old TASK helpers can become shadowed dead code when a later task re-implements them inline — review merged results.

## Tests

```powershell
venv\Scripts\python.exe -m pytest
```

370+ tests; live-run regressions get a test named after the run id that exposed them.
