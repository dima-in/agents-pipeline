from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from colorama import Fore, Style, init

init(autoreset=True)


class WorkflowLogger:
    def __init__(self, log_dir: str = ".openclaw/logs") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = timestamp
        self.run_dir = self.log_dir / f"run_{timestamp}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"workflow_{timestamp}.log"
        self.json_file = self.log_dir / f"workflow_{timestamp}.json"
        self.json_events: list[dict[str, Any]] = []

        self.logger = logging.getLogger(f"AgentsPipelineWorkflow-{timestamp}")
        self.logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler(self.log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.logger.addHandler(handler)
        self.logger.propagate = False

    def phase_start(self, phase_name: str, description: str = "") -> None:
        self._console(Fore.CYAN + Style.BRIGHT, f"== Фаза: {phase_name} ==")
        if description:
            self._console(Fore.CYAN, description)
        self.logger.info("PHASE_START: %s", phase_name)
        self._log_json("phase_start", {"phase": phase_name, "description": description})

    def phase_end(self, phase_name: str, status: str = "success") -> None:
        color = Fore.GREEN if status == "success" else Fore.RED
        status_label = self._translate_status(status)
        self._console(color, f"== Фаза завершена: {phase_name} [{status_label}] ==")
        self.logger.info("PHASE_END: %s - %s", phase_name, status)
        self._log_json("phase_end", {"phase": phase_name, "status": status})

    def agent_start(self, agent_name: str, task: str = "") -> None:
        self._console(Fore.YELLOW + Style.BRIGHT, f"Агент запущен: {agent_name}")
        if task:
            self._console(Fore.YELLOW, f"  Задача: {task}")
        self.logger.info("AGENT_START: %s - %s", agent_name, task)
        self._log_json("agent_start", {"agent": agent_name, "task": task})

    def agent_progress(self, agent_name: str, message: str) -> None:
        self._console(Fore.YELLOW, f"  {agent_name}: {message}")
        self.logger.debug("AGENT_PROGRESS: %s - %s", agent_name, message)

    def agent_end(self, agent_name: str, status: str = "success", result: str = "") -> None:
        color = Fore.GREEN if status == "success" else Fore.RED if status in {"failed", "timeout", "invalid_output"} else Fore.YELLOW
        status_label = self._translate_status(status)
        self._console(color, f"Агент завершен: {agent_name} [{status_label}]")
        if result:
            self._console(color, f"  Результат: {result}")
        self.logger.info("AGENT_END: %s - %s", agent_name, status)
        self._log_json("agent_end", {"agent": agent_name, "status": status, "result": result})

    def git_operation(self, operation: str, details: str = "") -> None:
        self._console(Fore.MAGENTA, f"Git: {operation} {details}".rstrip())
        self.logger.info("GIT: %s - %s", operation, details)
        self._log_json("git_operation", {"operation": operation, "details": details})

    def info(self, message: str) -> None:
        self._console(Fore.WHITE, message)
        self.logger.info("INFO: %s", message)

    def operator_box(self, title: str, lines: list[str] | None = None, *, color: str = "cyan") -> None:
        palette = {
            "cyan": Fore.CYAN,
            "green": Fore.GREEN,
            "yellow": Fore.YELLOW,
            "magenta": Fore.MAGENTA,
            "red": Fore.RED,
            "white": Fore.WHITE,
        }
        selected = palette.get(color, Fore.CYAN)
        clean_title = str(title or "").strip()
        self._console(selected + Style.BRIGHT, f"+-- {clean_title}")
        for line in lines or []:
            self._console(selected, f"| {line}")
        self._console(selected + Style.BRIGHT, "+--")
        self.logger.info("OPERATOR_BOX: %s - %s", clean_title, " | ".join(str(line) for line in (lines or [])))

    def warning(self, message: str) -> None:
        self._console(Fore.YELLOW, message)
        self.logger.warning("WARNING: %s", message)

    def error(self, message: str, details: str = "") -> None:
        self._console(Fore.RED + Style.BRIGHT, message)
        if details:
            self._console(Fore.RED, details)
        self.logger.error("ERROR: %s - %s", message, details)
        self._log_json("error", {"message": message, "details": details})

    def save_summary(self) -> Path:
        summary = self.log_dir / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        errors = [e for e in self.json_events if e["type"] == "error"]
        phases = [e for e in self.json_events if e["type"] == "phase_start"]
        agents = [e for e in self.json_events if e["type"] == "agent_end"]
        run_summary = self.save_run_summary()
        run_payload = json.loads(run_summary.read_text(encoding="utf-8")) if run_summary.exists() else {}
        summary.write_text(
            "\n".join(
                [
                    "Сводка workflow agents-pipeline",
                    f"phases={len(phases)}",
                    f"agents={len(agents)}",
                    f"errors={len(errors)}",
                    f"run_total_tokens={run_payload.get('total_tokens', 0)}",
                    f"run_estimated_cost_usd={run_payload.get('estimated_cost_usd', 0.0)}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return summary

    def save_agent_report(self, phase: str, agent_name: str, payload: dict[str, Any]) -> tuple[Path, Path]:
        agent_dir = self.run_dir / "agents" / phase
        agent_dir.mkdir(parents=True, exist_ok=True)
        base_name = self._safe_name(agent_name)
        json_path = agent_dir / f"{base_name}.json"
        md_path = agent_dir / f"{base_name}.md"

        payload = {
            "timestamp": datetime.now().isoformat(),
            "agent_name": agent_name,
            "phase": phase,
            **payload,
        }
        payload["usage"] = self._normalize_usage(payload.get("usage"))

        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        sections = [
            f"# {agent_name}",
            "",
            f"- timestamp: {payload.get('timestamp', '')}",
            f"- agent_name: {payload.get('agent_name', '')}",
            f"- phase: {payload.get('phase', '')}",
            f"- status: {payload.get('status', '')}",
            f"- result: {payload.get('result', '')}",
            f"- elapsed_s: {payload.get('elapsed_s', '')}",
            f"- returncode: {payload.get('returncode', '')}",
            f"- provider: {payload.get('runtime', {}).get('provider', '')}",
            f"- model: {payload.get('runtime', {}).get('model', '')}",
            f"- thinking: {payload.get('runtime', {}).get('thinking', '')}",
            "",
            "## Usage",
            "",
            f"- input_tokens: {payload.get('usage', {}).get('input_tokens', '')}",
            f"- output_tokens: {payload.get('usage', {}).get('output_tokens', '')}",
            f"- total_tokens: {payload.get('usage', {}).get('total_tokens', '')}",
            f"- estimated_cost_usd: {payload.get('usage', {}).get('estimated_cost_usd', '')}",
            f"- usage_status: {payload.get('usage', {}).get('usage_status', '')}",
            "",
            "## Command",
            "",
            "```text",
            payload.get("command", ""),
            "```",
            "",
            "## Prompt Message",
            "",
            "```text",
            payload.get("message", ""),
            "```",
            "",
            "## System Prompt",
            "",
            "```text",
            payload.get("system_message", ""),
            "```",
            "",
            "## User Task",
            "",
            "```text",
            payload.get("user_message", ""),
            "```",
            "",
            "## Developer Repair Feedback",
            "",
            "```text",
            payload.get("developer_feedback_text", ""),
            "```",
            "",
            "## Parsed Output",
            "",
            "```text",
            payload.get("parsed_output", ""),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            payload.get("stdout", ""),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            payload.get("stderr", ""),
            "```",
        ]
        md_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
        return md_path, json_path

    def save_phase_summary(self, phase: str, phase_name: str = "") -> Path:
        agent_dir = self.run_dir / "agents" / phase
        summary_path = self.run_dir / f"{phase}-summary.md"
        json_summary_path = self.run_dir / "phase_summary.json"
        if not agent_dir.exists():
            payload = {
                "phase": phase,
                "phase_name": phase_name or phase,
                "completed_agents": 0,
                "failed_agents": 0,
                "total_elapsed_s": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "agent_statuses": [],
            }
            json_summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_path.write_text(f"# {phase_name or phase} Summary\n\nNo agent reports found.\n", encoding="utf-8")
            return summary_path

        reports = self._load_agent_reports(phase)
        usage_totals = self._build_usage_totals(reports)

        sections = [f"# {phase_name or phase} Summary", ""]
        for report in reports:
            sections.extend(
                [
                    f"## {report.get('agent', 'agent')}",
                    "",
                    f"- status: {report.get('status', '')}",
                    f"- result: {report.get('result', '')}",
                    f"- elapsed_s: {report.get('elapsed_s', '')}",
                    f"- provider: {report.get('runtime', {}).get('provider', '')}",
                    f"- model: {report.get('runtime', {}).get('model', '')}",
                    f"- total_tokens: {report.get('usage', {}).get('total_tokens', '')}",
                    f"- estimated_cost_usd: {report.get('usage', {}).get('estimated_cost_usd', '')}",
                    f"- usage_status: {report.get('usage', {}).get('usage_status', '')}",
                    "",
                    "### Parsed Output",
                    "",
                    "```text",
                    report.get("parsed_output", ""),
                    "```",
                    "",
                ]
            )

        payload = {
            "phase": phase,
            "phase_name": phase_name or phase,
            "completed_agents": sum(1 for report in reports if report.get("status") == "success"),
            "failed_agents": sum(1 for report in reports if report.get("status") != "success"),
            "total_elapsed_s": round(sum(float(report.get("elapsed_s") or 0.0) for report in reports), 2),
            "input_tokens": usage_totals["input_tokens"],
            "output_tokens": usage_totals["output_tokens"],
            "total_tokens": usage_totals["total_tokens"],
            "estimated_cost_usd": usage_totals["estimated_cost_usd"],
            "agent_statuses": [
                {
                    "agent_name": report.get("agent_name") or report.get("agent"),
                    "status": report.get("status"),
                    "elapsed_s": report.get("elapsed_s"),
                    "total_tokens": report.get("usage", {}).get("total_tokens"),
                    "estimated_cost_usd": report.get("usage", {}).get("estimated_cost_usd"),
                    "usage_status": report.get("usage", {}).get("usage_status"),
                }
                for report in reports
            ],
        }
        json_summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_path.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
        return summary_path

    def save_run_summary(self) -> Path:
        reports = self._load_agent_reports()
        payload = self._build_run_summary_payload(reports)
        summary_path = self.run_dir / "run_summary.json"
        summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary_path

    def get_phase_totals(self, phase: str) -> dict[str, int | float]:
        return self._build_usage_totals(self._load_agent_reports(phase))

    def get_run_totals(self) -> dict[str, int | float]:
        return self._build_usage_totals(self._load_agent_reports())

    def _load_agent_reports(self, phase: str | None = None) -> list[dict[str, Any]]:
        agents_root = self.run_dir / "agents"
        if not agents_root.exists():
            return []

        if phase is None:
            report_paths = sorted(agents_root.glob("*/*.json"))
        else:
            report_paths = sorted((agents_root / phase).glob("*.json"))

        reports: list[dict[str, Any]] = []
        for report_path in report_paths:
            try:
                reports.append(json.loads(report_path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return reports

    @staticmethod
    def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
        usage = usage or {}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "usage_status": usage.get("usage_status", "unavailable"),
        }

    @staticmethod
    def _build_usage_totals(reports: list[dict[str, Any]]) -> dict[str, int | float]:
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        estimated_cost = 0.0
        for report in reports:
            usage = report.get("usage", {}) or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)
            estimated_cost += float(usage.get("estimated_cost_usd") or 0.0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
        }

    def _build_run_summary_payload(self, reports: list[dict[str, Any]]) -> dict[str, Any]:
        totals = self._build_usage_totals(reports)
        return {
            "completed_agents": sum(1 for report in reports if report.get("status") == "success"),
            "failed_agents": sum(1 for report in reports if report.get("status") != "success"),
            "total_elapsed_s": round(sum(float(report.get("elapsed_s") or 0.0) for report in reports), 2),
            **totals,
        }

    def _log_json(self, event_type: str, data: dict[str, Any]) -> None:
        event = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "data": data,
        }
        self.json_events.append(event)
        self.json_file.write_text(
            json.dumps(self.json_events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _translate_status(status: str) -> str:
        fixed_mapping = {
            "success": "успех",
            "failed": "ошибка",
            "rejected": "отклонено",
            "timeout": "таймаут",
            "invalid_output": "некорректный вывод",
        }
        return fixed_mapping.get(status, status)
        mapping = {
            "success": "успех",
            "failed": "ошибка",
            "rejected": "отклонено",
            "timeout": "таймаут",
            "invalid_output": "некорректный вывод",
        }
        return mapping.get(status, status)

    @staticmethod
    def _safe_name(value: str) -> str:
        result = []
        for char in value.lower():
            if char.isalnum() or char in {"-", "_"}:
                result.append(char)
            else:
                result.append("-")
        return "".join(result).strip("-") or "agent"

    @staticmethod
    def _console(color: str, message: str) -> None:
        replacements = {
            "Р¤Р°Р·Р° Р·Р°РІРµСЂС€РµРЅР°": "Фаза завершена",
            "Р¤Р°Р·Р°": "Фаза",
            "РђРіРµРЅС‚ Р·Р°РїСѓС‰РµРЅ": "Агент запущен",
            "РђРіРµРЅС‚ Р·Р°РІРµСЂС€РµРЅ": "Агент завершен",
            "Р—Р°РґР°С‡Р°": "Задача",
            "Р РµР·СѓР»СЊС‚Р°С‚": "Результат",
        }
        for broken, fixed in replacements.items():
            message = message.replace(broken, fixed)
        print(color + message + Style.RESET_ALL)
