#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from workflow.orchestrator import WorkflowOrchestrator


def configure_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agents-pipeline multi-agent workflow")
    parser.add_argument(
        "--mode",
        choices=["interactive", "auto"],
        default="interactive",
        help="Execution mode",
    )
    parser.add_argument(
        "--phase",
        choices=["research", "implementation", "deployment", "full"],
        default="full",
        help="Phase to run",
    )
    parser.add_argument(
        "--config",
        default="workflow/config.yaml",
        help="Path to workflow config",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Only validate runtime configuration and exit",
    )
    parser.add_argument(
        "--skip-git",
        action="store_true",
        help="Disable branch/merge actions",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Console log level",
    )
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()

    orchestrator = WorkflowOrchestrator(config_path=args.config)
    orchestrator.config["workflow"]["mode"] = args.mode
    orchestrator.config["git"]["enabled"] = not args.skip_git
    orchestrator.config["logging"]["level"] = args.log_level

    if args.preflight_only:
        return 0 if orchestrator._preflight_runtime() else 1

    if args.phase == "research":
        ok = orchestrator.run_research_phase()
    elif args.phase == "implementation":
        ok = orchestrator.run_implementation_phase()
    elif args.phase == "deployment":
        ok = orchestrator.run_deployment_phase()
    else:
        ok = orchestrator.run_full_cycle()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
