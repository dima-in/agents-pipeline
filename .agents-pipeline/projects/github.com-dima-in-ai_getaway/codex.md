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
