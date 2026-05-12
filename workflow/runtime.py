from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RuntimeConfig:
    executor: str
    runner_bin: str
    provider: str
    model: str
    profile: str
    preflight_enabled: bool
    require_registry_preflight: bool
    require_model_list_preflight: bool
    run_mode: str
    thinking: str
    workspace: str

    @property
    def env_overrides(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.provider:
            env["OPENCLAW_PROVIDER"] = self.provider
        if self.model:
            env["OPENCLAW_MODEL"] = self.model
        if self.profile:
            env["OPENCLAW_PROFILE"] = self.profile
        return env


def load_runtime_config(
    settings_path: str = ".openclaw/config/settings.yaml",
    *,
    engine_root: str | Path | None = None,
    workflow_settings: dict[str, Any] | None = None,
    workspace: str | Path | None = None,
) -> RuntimeConfig:
    engine_base = Path(engine_root).resolve() if engine_root is not None else Path.cwd().resolve()
    settings_file = Path(settings_path)
    if not settings_file.is_absolute():
        settings_file = engine_base / settings_file
    settings = {}
    if settings_file.exists():
        settings = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
    loaded_workflow_settings = workflow_settings
    if loaded_workflow_settings is None:
        workflow_settings_path = engine_base / "workflow/config.yaml"
        loaded_workflow_settings = {}
        if workflow_settings_path.exists():
            loaded_workflow_settings = yaml.safe_load(workflow_settings_path.read_text(encoding="utf-8")) or {}

    openclaw_settings = settings.get("openclaw", {})
    project_settings = settings.get("project", {})
    workflow_settings_block = loaded_workflow_settings.get("workflow", {})
    workflow_runtime = loaded_workflow_settings.get("runtime", {})
    resolved_workspace = str(Path(workspace).resolve()) if workspace is not None else str(project_settings.get("workspace", "."))
    return RuntimeConfig(
        executor=str(workflow_settings_block.get("executor", "openclaw")),
        runner_bin=os.getenv("OPENCLAW_BIN", openclaw_settings.get("bin", "openclaw")),
        provider=os.getenv("OPENCLAW_PROVIDER", workflow_runtime.get("provider", openclaw_settings.get("provider", "openrouter"))),
        model=os.getenv("OPENCLAW_MODEL", workflow_runtime.get("model", openclaw_settings.get("model", "openrouter/auto"))),
        profile=os.getenv("OPENCLAW_PROFILE", openclaw_settings.get("profile", "default")),
        preflight_enabled=bool(openclaw_settings.get("preflight_enabled", True)),
        require_registry_preflight=bool(workflow_settings_block.get("require_registry_preflight", os.name != "nt")),
        require_model_list_preflight=bool(workflow_settings_block.get("require_model_list_preflight", os.name != "nt")),
        run_mode=str(openclaw_settings.get("run_mode", "local")),
        thinking=str(workflow_runtime.get("thinking", openclaw_settings.get("thinking", "medium"))),
        workspace=resolved_workspace,
    )


def resolve_runner_path(runner_bin: str) -> str | None:
    if Path(runner_bin).exists():
        return str(Path(runner_bin))
    return shutil.which(runner_bin)


def required_key_env(provider: str) -> str | None:
    normalized = provider.strip().lower()
    if normalized == "openrouter":
        return "OPENROUTER_API_KEY"
    if normalized in {"claude", "anthropic"}:
        return "ANTHROPIC_API_KEY"
    return None


def has_provider_credentials(provider: str) -> bool:
    required_env = required_key_env(provider)
    if required_env and os.getenv(required_env):
        return True

    openclaw_home = Path.home() / ".openclaw"
    auth_profiles = openclaw_home / "agents" / "main" / "agent" / "auth-profiles.json"
    openclaw_json = openclaw_home / "openclaw.json"

    if auth_profiles.exists():
        data = yaml.safe_load(auth_profiles.read_text(encoding="utf-8")) or {}
        profiles = data.get("profiles", {})
        normalized = provider.strip().lower()
        if normalized == "claude":
            normalized = "anthropic"
        for profile in profiles.values():
            if str(profile.get("provider", "")).strip().lower() == normalized:
                return True

    if openclaw_json.exists():
        data = yaml.safe_load(openclaw_json.read_text(encoding="utf-8")) or {}
        profiles = data.get("auth", {}).get("profiles", {})
        normalized = provider.strip().lower()
        if normalized == "claude":
            normalized = "anthropic"
        for profile in profiles.values():
            if str(profile.get("provider", "")).strip().lower() == normalized:
                return True

    return False
