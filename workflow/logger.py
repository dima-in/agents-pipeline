from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from colorama import Fore, Style, init

init(autoreset=True)


class WorkflowLogger:
    def __init__(self, log_dir: str = ".openclaw/logs") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"workflow_{timestamp}.log"
        self.json_file = self.log_dir / f"workflow_{timestamp}.json"
        self.json_events: list[dict] = []

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
        color = Fore.GREEN if status == "success" else Fore.RED if status == "failed" else Fore.YELLOW
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
        summary.write_text(
            "\n".join(
                [
                    "Сводка workflow agents-pipeline",
                    f"phases={len(phases)}",
                    f"agents={len(agents)}",
                    f"errors={len(errors)}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return summary

    def _log_json(self, event_type: str, data: dict) -> None:
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
        mapping = {
            "success": "успех",
            "failed": "ошибка",
            "rejected": "отклонено",
            "timeout": "таймаут",
        }
        return mapping.get(status, status)

    @staticmethod
    def _console(color: str, message: str) -> None:
        print(color + message + Style.RESET_ALL)
