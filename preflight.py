from __future__ import annotations

import argparse
import sys

from workflow.runtime import has_provider_credentials, load_runtime_config, required_key_env, resolve_runner_path


def run_preflight() -> int:
    runtime = load_runtime_config()
    runner_path = resolve_runner_path(runtime.runner_bin)
    required_key = required_key_env(runtime.provider)

    print("agents-pipeline preflight")
    print(f"runner_bin={runtime.runner_bin}")
    print(f"provider={runtime.provider}")
    print(f"model={runtime.model}")
    print(f"profile={runtime.profile}")
    print(f"workspace={runtime.workspace}")
    print(f"runner_resolved={runner_path or 'missing'}")
    if required_key:
        print(f"{required_key}_or_home_profile={'available' if has_provider_credentials(runtime.provider) else 'missing'}")

    if not runner_path:
        print("status=failed")
        print("reason=runner_missing")
        return 1
    if required_key and not has_provider_credentials(runtime.provider):
        print("status=failed")
        print("reason=provider_key_missing")
        return 1

    print("status=ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate OpenClaw runtime configuration")
    parser.parse_args()
    return run_preflight()


if __name__ == "__main__":
    sys.exit(main())
