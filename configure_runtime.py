from __future__ import annotations

import argparse
from pathlib import Path

import yaml


DEFAULT_MODELS = {
    "openrouter": "openrouter/auto",
    "claude": "anthropic/claude-sonnet-4-5",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure project-level OpenClaw runtime settings")
    parser.add_argument("--provider", choices=["openrouter", "claude"], required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--runner-bin", default=None)
    parser.add_argument("--profile", default="default")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings_path = Path(".openclaw/config/settings.yaml")
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    settings = settings or {}
    openclaw_settings = settings.setdefault("openclaw", {})
    openclaw_settings["provider"] = args.provider
    openclaw_settings["model"] = args.model or DEFAULT_MODELS[args.provider]
    openclaw_settings["profile"] = args.profile
    if args.runner_bin:
        openclaw_settings["bin"] = args.runner_bin
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), encoding="utf-8")

    print(f"provider={openclaw_settings['provider']}")
    print(f"model={openclaw_settings['model']}")
    print(f"profile={openclaw_settings['profile']}")
    print(f"runner_bin={openclaw_settings.get('bin', 'openclaw')}")
    print("note=Project settings updated. Add provider keys through environment variables or .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
