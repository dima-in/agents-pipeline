# Codex Project Context

Project:
- `ai_getaway` / local workspace `vps`
- product is an AI gateway with React frontend and FastAPI backend

Repository shape:
- `frontend/` is the client application
- `gateway-v4/` is the backend service
- backend includes routing, proxying, chat flows, auth, billing, admin, RAG, and Alembic migrations

Runtime and infra:
- primary provider flow is OpenRouter
- backend also has direct provider paths for OpenAI, Anthropic, DeepSeek, Qwen, and Ollama
- prod backend path on server: `/opt/ai-gateway/gateway-v4/`
- prod frontend path on server: `/opt/ai-gateway/frontend/dist/`
- prod secrets live on VPS in `/opt/ai-gateway/gateway-v4/.env` and must not be committed
- production deploy uses `gateway-v4/docker-compose.prod.yml`

Operational rules:
- after backend `.py` changes, expect backend restart or container recreate on server
- after frontend changes, frontend must be rebuilt and redeployed
- migrations matter; check `gateway-v4/alembic/versions/` when changing data model
- do not move secrets into repo or GitHub Actions

Current development focus:
- backend routing / proxy / monitoring work is the preferred safe area
- be careful around `billing`, `auth`, deploy config, and production secrets
- preserve multi-provider model routing behavior and existing chat flows unless task explicitly changes them

Implementation pipeline notes:
- `developer` now gets automatic repair feedback after deterministic validation failures and after valid QA failures
- if `developer` claims `status=no_changes` / "already complete", the pipeline runs forced deterministic validation before accepting that claim
- if `developer` uses forbidden broad retrieval like `list_files` in implementation mode, the next turn includes exact-path repair instructions instead of immediately killing the attempt
- per-attempt feedback files live under `.openclaw/feedback/<project_id>/<run_id>/attempt_<n>/developer.md` and `qa.md`
- operator preference: keep console output compact; show prompt brief and feedback preview in console, but keep full heavy prompt/planner internals only in report/log files

<!-- AUTO-GENERATED:RUN-CONTEXT START -->
## Auto-updated Run Context

- updated_at: 2026-05-27T17:41:08
- last_run_id: run_20260527_173519
- phases_touched: implementation
- completed_agents: 7
- failed_agents: 1
- total_tokens: 26539
- estimated_cost_usd: 0.104175

### Current Selected Implementation Task

- id: TASK-001
- scope: backend-only
<!-- AUTO-GENERATED:RUN-CONTEXT END -->
