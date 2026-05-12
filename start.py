#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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
        "--workspace",
        default=None,
        help="Target workspace path to analyze",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Optional stable target project id override",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Do not load previous summaries or memory context",
    )
    parser.add_argument(
        "--fresh-run",
        action="store_true",
        help="Do not reuse any previous run context",
    )
    parser.add_argument(
        "--task-scope",
        default=None,
        help="Explicit implementation task scope override",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Implementation backlog item id or 1-based number",
    )
    parser.add_argument(
        "--list-tasks",
        action="store_true",
        help="List implementation backlog items and exit",
    )
    parser.add_argument(
        "--next-task",
        action="store_true",
        help="Select the next uncompleted implementation backlog item",
    )
    parser.add_argument(
        "--research-run",
        default=None,
        help="Specific research run id to use for implementation context",
    )
    parser.add_argument(
        "--allow-scope-expansion",
        action="store_true",
        help="Bypass implementation scope watchdog with a warning",
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


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    engine_root = Path(__file__).resolve().parent
    launch_cwd = Path(os.environ.get("AGENTS_PIPELINE_LAUNCH_CWD") or os.getcwd()).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = engine_root / config_path

    orchestrator = WorkflowOrchestrator(
        config_path=str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(launch_cwd),
        workspace=args.workspace,
        project_id=args.project_id,
        no_memory=args.no_memory,
        fresh_run=args.fresh_run,
        task_scope=args.task_scope,
        selected_task_ref=args.task_id,
        next_task=args.next_task,
        research_run=args.research_run,
        allow_scope_expansion=args.allow_scope_expansion,
    )
    orchestrator.config["workflow"]["mode"] = args.mode
    orchestrator.config["git"]["enabled"] = not args.skip_git
    orchestrator.config["logging"]["level"] = args.log_level

    if args.list_tasks:
        return orchestrator.print_implementation_backlog()

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
