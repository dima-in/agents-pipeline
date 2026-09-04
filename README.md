# agents-pipeline

`agents-pipeline` is a local-first, multi-phase agent workflow that autonomously implements backlog tasks INTO a target repository. It is **universal**: the engine names zero projects; every fact about the target (language, persistence model, schema, join paths, layout, conventions) is detected from the target repo itself or filled in by a profiler agent. The same engine onboards a SQLAlchemy/Alembic service and a hand-written raw-MySQL FastAPI app without code changes.

It scales from a single task to a big ambition: a **product-strategist** agent decomposes a vision into a prioritized, dependency-linked **roadmap of slices** (each slice = one shippable feature), and a slice driver feeds the next slice's goal to the planner → backlog → implementation loop. A **supervisor layer** (diagnostician + arbiter + escalation card) keeps a stuck run from looping expensively or dying cryptically, and application endpoints are covered by **real behavioral tests** (FastAPI `TestClient` + mocked boundaries) run in the target's own virtualenv.

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

Or drive a big vision as a roadmap of slices:

```powershell
venv\Scripts\python.exe start.py --build-roadmap --mode auto --workspace D:\SomeProject --goal "<the vision>"
venv\Scripts\python.exe start.py --list-slices  --workspace D:\SomeProject          # inspect the roadmap
venv\Scripts\python.exe start.py --next-slice   --mode auto --workspace D:\SomeProject   # plan + implement the next slice
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
- `state/roadmap.json` — the slice roadmap (product-strategist output)

`project_id` derives from the target's git remote (e.g. `github.com-user-repo`).

## Phases and agents

**Research** (once per goal): project-analyst, competitor/market/tech-analyst, innovation-scout, product-manager, **product-strategist**. Produces requirements, a backlog, and a slice roadmap. Each role gets a tailored context profile and a deterministic ≤2000-char handoff summary for the next agent. `product-strategist` reads the vision + codebase and emits `roadmap.json` (see *Roadmap / slice decomposition*).

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

## Roadmap / slice decomposition

One level above the planner. The `product-strategist` agent turns a big VISION into an ordered list of **slices** — each slice a coherent, shippable feature (e.g. "natural-language order entry", "weekly digest"), with `id`, `goal`, `rationale`, `depends_on`, `status`, `value`, `effort`. It recognizes what already exists in the codebase and marks those slices `done` instead of re-proposing them. The roadmap persists to `state/roadmap.json`.

The slice driver then runs the pipeline slice by slice:

- `--build-roadmap` — run only the strategist (~$0.02) to (re)generate `roadmap.json` from the vision, without paying for the full research phase.
- `--list-slices` — print the roadmap (vision, per-slice goal/value/effort/deps/status).
- `--next-slice` — pick the next `pending` slice whose dependencies are `done` (or resume the `in_progress` one), set it as the active goal, regenerate the backlog for it, and run implementation. When the slice's backlog is fully completed it is marked `done`; run again for the next slice.

Slices reuse generic task ids (`TASK-001`...), so starting a new slice archives the previous slice's backlog and completed-task registry (state file **and** the settings list) — otherwise a prior slice's completed ids would make the new slice's first tasks look already-done and selection would skip ahead.

## Grounding (why agents don't hallucinate the stack)

- **Architecture Profile** — confidence-tagged facts `{verified|inferred|unknown}` about the CURRENT codebase: language, persistence (engine, access style, sync/async, session pattern), migrations, layout, tests root. Deterministic detectors fill what they can (`verified`); the profiler agent fills gaps (`inferred`); verified facts are never overridden. The architect's intended END-STATE is captured and carried across tasks as the Target architecture.
- **DB schema ground truth** — for raw-SQL repos, `CREATE TABLE` statements are parsed into exact table/column lists **and FOREIGN KEY join paths**. Planners see which tables are linked and which are NOT ("the ONLY declared join paths"), so a metric that needs an impossible join gets narrowed by the architect itself instead of shipping invented columns.
- **DB cursor row shape** — the engine detects whether raw cursors yield tuples (plain `conn.cursor()`) or dicts (`dictionary=True` / `DictCursor`) and states it as shared ground truth. Both the code-developer (which reads rows) and the test-developer (which mocks them) then agree — a plain cursor returns tuples read by index (`row[0]`), so `row['col']` (which raises `tuple indices must be integers`) is caught before it ships.
- **Sync/async guardrail** — AST detection of the real DB concurrency + injected-session pattern; contracts demanding async DB on a sync stack are rejected at design time.
- **Evidence-checked obsolescence** — when the task-designer claims a task is "already implemented", the engine extracts checkable tokens (URLs, camelCase/snake_case identifiers) from the acceptance criteria and greps the task's files; a false claim gets an explicit disproof in the retry feedback.

## Deterministic gates (fail-closed)

- `must_contain` gate: def/class symbols matched by AST name, decorators (e.g. `@app.get("/api/...")`) by quote/whitespace-normalized substring against the FULL file — immune to truncated read excerpts. Runs both post-hoc and **within-turn** (a developer cannot finalize while required symbols are missing).
- Write-reserved turns: reads can spend the retrieval budget, but edit agents always keep extra turns where read requests are bounced with a force-write instruction — reads can no longer starve the write.
- Truncated-write repair: a tool-request-looking final answer that failed to parse (e.g. a whole-file `write_file` cut by max_tokens) is never accepted; the loop demands small `apply_patch` hunks instead.
- QA/validator verdicts are **fail-closed**: an unrecognized verdict phrasing is a rejection by default; a validator report full of `**FAILED**` sections fails even without a status line. Import placement (module-level vs function-local) is non-blocking style, not a violation.
- Scope policy: per-project `forbidden_paths` (checked first, always win) + per-task `allowed_paths` (the only editable files). Sensitive files (billing/auth/payment...) are auto-derived from the repo map by keyword. Test-less tasks (frontend/docs/config) don't demand a test contract.

## Supervisor layer (don't loop, don't die cryptically)

After every failed attempt, before the git rollback erases the diff:

- **Diagnostician** — one cheap LLM judgement (default sonnet) of the verdict vs the ACTUAL diff + the full pytest traceback. It outputs `Диагноз: <QA прав | QA придирается | контракт некорректны> — <why>` plus one concrete recommendation, which is auto-appended to the developer's repair feedback for the next attempt. Information only — never a gate.
- **Arbiter** (`workflow.supervisor_arbiter: auto|ask`) — overrules a taste-level LLM rejection **only** when every objective signal already says done. It accepts a `qa_failed` when the deterministic gates are green and the diagnosis is "QA придирается" (style, e.g. import placement); and a `template_validation_failed` when the deterministic gates are green, QA already **passed**, and the diagnosis flags no real defect (the tertiary template reviewer is the lone objector). Deterministic gates (pytest / must_contain / scope) stay absolute — a real failure is never accepted.
- **Escalation card** — when retries are exhausted or a hard status blocks, one actionable card ("Требуется решение") states the task, attempts, the recurring blocker, the diagnosis, and concrete decisions — instead of a silent expensive loop.

## Behavioral tests for the target

For application/API code the test-developer writes **real behavioral tests**: it adds the repo root to `sys.path`, imports the app, drives it with FastAPI `TestClient`, and asserts actual HTTP status codes and JSON — mocking only the external boundaries (`mock.patch("main.requests.post")`, `mock.patch("main.UseDatabase")`). Migration files stay on the static-AST path (parse, never execute). These tests run in the **target's own virtualenv**: `resolve_python_executable` prefers `target_workspace/.venv` (which has the app's dependencies) over the engine venv, so `import main` and the app's stack resolve. For the mocks to bind, the code-developer imports external boundaries at module level (so `main.<name>` exists).

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

Per-agent/phase/run cost is printed after every agent and accumulated from saved reports; the phase stops before scheduling more agents once the limit is exceeded. Accounting is honest — **every** OpenRouter request (retrieval turns, retries, repairs) is counted, not just the final response. For Claude models the engine sends Anthropic **prompt caching** (`cache_control: ephemeral`) on message content, and the cost line shows the input cache-hit rate (`| кэш входа N%`); reusing the profiler/architect/planner across tasks of an unchanged project cuts per-task cost further.

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
--build-roadmap            run only product-strategist; (re)generate roadmap.json and exit
--list-slices              print the slice roadmap and exit
--next-slice               drive the next roadmap slice (set goal, plan backlog, implement)
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

410+ tests; live-run regressions get a test named after the run id that exposed them.
