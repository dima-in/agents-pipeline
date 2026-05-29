from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


def find_latest_implementation_run(log_dir: Path) -> Path | None:
    for run_dir in sorted(Path(log_dir).glob("run_*"), reverse=True):
        if (run_dir / "human_report.md").exists() or (run_dir / "agents" / "implementation").exists():
            return run_dir
    return None


def generate_human_report(
    run_dir: Path,
    *,
    project_id: str = "",
    target_workspace: str | Path | None = None,
    feedback_root: str | Path | None = None,
    final_status: str = "",
) -> Path:
    explainer = RunExplainer(
        Path(run_dir),
        project_id=project_id,
        target_workspace=Path(target_workspace).resolve() if target_workspace else None,
        feedback_root=Path(feedback_root).resolve() if feedback_root else None,
        final_status=final_status,
    )
    return explainer.write()


class RunExplainer:
    def __init__(
        self,
        run_dir: Path,
        *,
        project_id: str = "",
        target_workspace: Path | None = None,
        feedback_root: Path | None = None,
        final_status: str = "",
    ) -> None:
        self.run_dir = run_dir
        self.project_id = project_id or run_dir.parent.name
        self.run_id = run_dir.name
        self.feedback_root = feedback_root
        self.final_status_override = final_status
        self.reports = self._load_reports()
        inferred_workspace = self._first_report_value("target_workspace")
        self.target_workspace = target_workspace or (Path(inferred_workspace).resolve() if inferred_workspace else None)
        self.selected_task = self._selected_task()
        self.feedback_files = self._feedback_files()

    def write(self) -> Path:
        path = self.run_dir / "human_report.md"
        path.write_text(self.render(), encoding="utf-8")
        return path

    def render(self) -> str:
        sections: list[str] = [
            "# Отчёт по запуску implementation",
            "",
            *self._summary_section(),
            "",
            *self._contract_section(),
            "",
        ]
        for report_path, report in self.reports:
            sections.extend(self._agent_section(report_path, report))
            sections.append("")
        sections.extend(self._code_changes_section())
        sections.append("")
        sections.extend(self._checks_section())
        sections.append("")
        sections.extend(self._next_steps_section())
        sections.append("")
        return "\n".join(sections)

    def _load_reports(self) -> list[tuple[Path, dict[str, Any]]]:
        agent_dir = self.run_dir / "agents" / "implementation"
        reports: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(agent_dir.glob("*.json")):
            payload = self._read_json(path)
            if isinstance(payload, dict):
                reports.append((path, payload))
        return sorted(reports, key=lambda item: str(item[1].get("timestamp") or item[0].name))

    def _selected_task(self) -> dict[str, Any]:
        for _path, report in reversed(self.reports):
            contract = report.get("selected_task_contract")
            if isinstance(contract, dict):
                return dict(contract)
        for _path, report in reversed(self.reports):
            task_id = str(report.get("selected_task_id") or "").strip()
            if task_id:
                return {
                    "id": task_id,
                    "title": str(report.get("selected_task_title") or report.get("title") or ""),
                    "allowed_paths": self._as_list(report.get("selected_task_allowed_paths")),
                    "forbidden_paths": self._as_list(report.get("forbidden_paths")),
                    "required_test_paths": self._as_list(report.get("required_test_paths")),
                    "acceptance_criteria": self._as_list(report.get("acceptance_criteria")),
                }
        return {}

    def _summary_section(self) -> list[str]:
        selected_task_id = str(self.selected_task.get("id") or self._first_report_value("selected_task_id") or "").strip()
        selected_task_title = str(self.selected_task.get("title") or "").strip()
        return [
            "## Сводка",
            "",
            f"- project_id: {self.project_id or 'неизвестно'}",
            f"- run_id: {self.run_id}",
            f"- target_workspace: {self.target_workspace or self._first_report_value('target_workspace') or 'неизвестно'}",
            f"- selected_task_id: {selected_task_id or 'не выбран'}",
            f"- selected_task_title: {selected_task_title or 'не указано'}",
            f"- selected_task_source: {self._first_report_value('selected_task_source') or 'не указано'}",
            f"- final_status: {self._final_status()}",
        ]

    def _contract_section(self) -> list[str]:
        report_values = self._last_report_with("selected_task_allowed_paths") or {}
        allowed = self._as_list(self.selected_task.get("allowed_paths")) or self._as_list(report_values.get("selected_task_allowed_paths"))
        forbidden = self._as_list(self.selected_task.get("forbidden_paths")) or self._as_list(report_values.get("forbidden_paths"))
        tests = self._as_list(self.selected_task.get("required_test_paths")) or self._as_list(report_values.get("required_test_paths"))
        acceptance = self._as_list(self.selected_task.get("acceptance_criteria")) or self._as_list(report_values.get("acceptance_criteria"))
        return [
            "## Контракт задачи",
            "",
            "- allowed_paths:",
            *self._bullet_values(allowed),
            "- forbidden_paths:",
            *self._bullet_values(forbidden),
            "- required_test_paths:",
            *self._bullet_values(tests),
            "- acceptance_criteria:",
            *self._bullet_values(acceptance),
        ]

    def _agent_section(self, report_path: Path, report: dict[str, Any]) -> list[str]:
        agent = str(report.get("agent_name") or report.get("agent") or report_path.stem)
        runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
        prompt_path = report_path.with_suffix(".md")
        tools = self._agent_tools(report)
        files_read = self._as_list(report.get("read_paths")) or self._as_list(report.get("files_read"))
        files_written = self._as_list(report.get("write_paths")) or self._as_list(report.get("files_written"))
        files_changed = self._as_list(report.get("developer_changed_files")) or self._as_list(report.get("changed_files"))
        return [
            f"## Агент: {agent}",
            "",
            f"- role: {agent}",
            f"- model/provider/thinking: {runtime.get('model', '') or 'не указано'} / {runtime.get('provider', '') or 'не указано'} / {runtime.get('thinking', '') or 'не указано'}",
            f"- task text: {self._short(report.get('user_message') or report.get('message') or report.get('result'))}",
            "- allowed paths:",
            *self._bullet_values(self._as_list(report.get("selected_task_allowed_paths"))),
            f"- execution mode: {report.get('execution_mode') or 'balanced'}",
            f"- retrieval budget: {report.get('retrieval_budget', 'не указано')}",
            f"- prompt file path: {prompt_path}",
            f"- response file path: {report_path}",
            f"- short prompt summary: {self._prompt_summary(report)}",
            f"- short response summary: {self._short(report.get('parsed_output') or report.get('result') or report.get('stdout'))}",
            "- tools used:",
            *self._bullet_values(tools),
            "- files read:",
            *self._bullet_values(files_read),
            "- files written:",
            *self._bullet_values(files_written),
            "- files changed:",
            *self._bullet_values(files_changed),
            f"- diff summary: {self._diff_summary_for_paths(files_changed or files_written)}",
            f"- validation result: {self._validation_result(report)}",
            f"- failure reason if any: {self._failure_reason(report)}",
            f"- next recommended action: {self._agent_next_action(report)}",
        ]

    def _code_changes_section(self) -> list[str]:
        changed = self._changed_files()
        lines = ["## Изменения в коде", ""]
        if not changed:
            lines.append("- изменений в коде не найдено в agent reports или текущем git diff")
            return lines
        for path in changed:
            lines.extend(
                [
                    f"### {path}",
                    "",
                    f"- change type: {self._change_type(path)}",
                    f"- short diff summary: {self._diff_summary_for_paths([path])}",
                    f"- important added symbols/classes/functions: {', '.join(self._added_symbols(path)) or 'не обнаружено'}",
                    "",
                ]
            )
        return lines

    def _checks_section(self) -> list[str]:
        developer_checks = self._report_by_agent("developer-checks")
        reports_with_scope = [report for _path, report in self.reports if report.get("scope_policy_result")]
        reports_with_contract = [report for _path, report in self.reports if "contract_compliance" in report or "contract_completeness" in report]
        rollback = self._rollback_summary()
        return [
            "## Проверки",
            "",
            f"- py_compile result: {self._check_line(developer_checks, 'py_compile')}",
            f"- pytest result: {self._check_line(developer_checks, 'pytest')}",
            f"- scope validation result: {self._scope_summary(reports_with_scope)}",
            f"- contract validation result: {self._contract_summary(reports_with_contract)}",
            f"- rollback result if any: {rollback or 'данных о rollback нет'}",
            f"- feedback files: {self._feedback_summary()}",
        ]

    def _next_steps_section(self) -> list[str]:
        status = self._final_status()
        failed = self._failed_reports()
        lines = ["## Что делать дальше", ""]
        if status == "success" or (self.reports and not failed and all(report.get("status") == "success" for _path, report in self.reports)):
            lines.extend(
                [
                    "- Статус выглядит успешным: проверь `git status`, затем сделай commit/push или выбери следующую задачу.",
                    "- Команда: `git status --short`",
                ]
            )
            return lines
        if failed:
            agent = str(failed[-1].get("agent_name") or failed[-1].get("agent") or "developer")
            lines.extend(
                [
                    f"- Исправить последний сбой агента `{agent}`: {self._failure_reason(failed[-1])}",
                    f"- Команда для продолжения: `python start.py --phase implementation --mode interactive --from-agent {agent}`",
                    f"- Repair focus: {self._repair_focus(failed[-1])}",
                ]
            )
            return lines
        lines.append("- Запуск ещё не дошёл до финального статуса. Продолжи текущую implementation фазу.")
        return lines

    def _first_report_value(self, key: str) -> Any:
        for _path, report in self.reports:
            value = report.get(key)
            if value not in (None, "", []):
                return value
        return ""

    def _last_report_with(self, key: str) -> dict[str, Any] | None:
        for _path, report in reversed(self.reports):
            if report.get(key) not in (None, "", []):
                return report
        return None

    def _final_status(self) -> str:
        if self.final_status_override:
            return self.final_status_override
        phase_status = self._phase_end_status()
        if phase_status:
            return phase_status
        failed = self._failed_reports()
        if failed:
            return str(failed[-1].get("status") or "failed")
        if self.reports and all(report.get("status") == "success" for _path, report in self.reports):
            return "success"
        return "in_progress"

    def _phase_end_status(self) -> str:
        json_path = self.run_dir.parent / f"workflow_{self.run_id.replace('run_', '')}.json"
        events = self._read_json(json_path)
        if not isinstance(events, list):
            return ""
        for event in reversed(events):
            if event.get("type") == "phase_end" and (event.get("data") or {}).get("phase"):
                return str((event.get("data") or {}).get("status") or "")
        return ""

    def _failed_reports(self) -> list[dict[str, Any]]:
        return [report for _path, report in self.reports if report.get("status") not in {"success", "", None}]

    def _report_by_agent(self, agent_name: str) -> dict[str, Any]:
        for _path, report in self.reports:
            if str(report.get("agent_name") or report.get("agent") or "") == agent_name:
                return report
        return {}

    def _agent_tools(self, report: dict[str, Any]) -> list[str]:
        tools = [
            *self._as_list(report.get("retrieval_tools_used")),
            *self._as_list(report.get("write_tools_used")),
        ]
        blocked = str(report.get("blocked_retrieval_tool") or "").strip()
        if blocked:
            tools.append(f"{blocked} (blocked)")
        return list(dict.fromkeys(tool for tool in tools if tool))

    def _prompt_summary(self, report: dict[str, Any]) -> str:
        stats = report.get("prompt_stats") if isinstance(report.get("prompt_stats"), dict) else {}
        summary = []
        if report.get("selected_task_id"):
            summary.append(f"task={report.get('selected_task_id')}")
        if report.get("selected_task_source"):
            summary.append(f"source={report.get('selected_task_source')}")
        if stats:
            summary.append(f"prompt_chars={stats.get('prompt_chars', 'n/a')}")
            summary.append(f"message_chars={stats.get('message_chars', 'n/a')}")
        return ", ".join(summary) or self._short(report.get("message") or report.get("user_message"))

    def _validation_result(self, report: dict[str, Any]) -> str:
        status = str(report.get("status") or "неизвестно")
        result = str(report.get("result") or "").strip()
        parts = [status]
        if result and result != status:
            parts.append(result)
        if report.get("scope_policy_result"):
            parts.append(f"scope={report.get('scope_policy_result')}")
        if "contract_compliance" in report:
            parts.append(f"contract_compliance={report.get('contract_compliance')}")
        if report.get("retrieval_hard_stop_triggered"):
            parts.append("retrieval_hard_stop=True")
        return "; ".join(parts)

    def _failure_reason(self, report: dict[str, Any]) -> str:
        status = str(report.get("status") or "").strip()
        if status in {"", "success"}:
            return "нет"
        details = [
            str(report.get("result") or "").strip(),
            str(report.get("last_write_error") or "").strip(),
            str(report.get("retrieval_limit_reason") or "").strip(),
            str(report.get("planner_rejection_reason") or "").strip(),
        ]
        return self._short(next((item for item in details if item), status), limit=260)

    def _agent_next_action(self, report: dict[str, Any]) -> str:
        status = str(report.get("status") or "")
        if status == "success":
            return "переходить к следующему агенту или финальным проверкам"
        if status == "strict_retrieval_blocked":
            return "сузить prompt/contract: агент должен писать из injected context или одного разрешённого read_file/read_files"
        if status == "no_changes":
            return "уточнить contract или заставить scoped write/apply_patch, если правка действительно нужна"
        if status:
            return "использовать failure reason и feedback файл для следующего retry"
        return "ожидать завершения агента"

    def _repair_focus(self, report: dict[str, Any]) -> str:
        if report.get("status") == "strict_retrieval_blocked":
            return "strict retrieval: убрать лишний read_file/read_files или расширить injected context"
        if report.get("status") == "no_changes":
            return "no_changes: проверить, почему нет write_file/apply_patch и есть ли реальная scoped правка"
        return self._failure_reason(report)

    def _changed_files(self) -> list[str]:
        paths: list[str] = []
        for _path, report in self.reports:
            paths.extend(self._as_list(report.get("developer_changed_files")))
            paths.extend(self._as_list(report.get("changed_files")))
            paths.extend(self._as_list(report.get("write_paths")))
        paths.extend(path for _status, path in self._git_status_paths())
        return sorted(dict.fromkeys(path for path in paths if path))

    def _git_status_paths(self) -> list[tuple[str, str]]:
        if not self.target_workspace:
            return []
        output = self._run_git(["status", "--porcelain", "-uall"])
        result: list[tuple[str, str]] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            status = line[:2].strip() or line[:2]
            path = line[3:].strip().replace("\\", "/")
            if path:
                result.append((status, path))
        return result

    def _change_type(self, path: str) -> str:
        status_by_path = {candidate: status for status, candidate in self._git_status_paths()}
        status = status_by_path.get(path, "")
        if "D" in status:
            return "deleted"
        if "A" in status or "??" in status:
            return "created"
        return "modified"

    def _diff_summary_for_paths(self, paths: list[str]) -> str:
        normalized = [path for path in paths if path]
        if not normalized or not self.target_workspace:
            return "нет данных"
        summaries = []
        for path in normalized[:6]:
            diff = self._run_git(["diff", "--", path])
            if not diff.strip():
                summaries.append(f"{path}: текущий git diff пуст")
                continue
            added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
            summaries.append(f"{path}: +{added}/-{removed}")
        return "; ".join(summaries) if summaries else "нет данных"

    def _added_symbols(self, path: str) -> list[str]:
        diff = self._run_git(["diff", "--", path])
        symbols: list[str] = []
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            code = line[1:].strip()
            match = re.match(r"(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", code)
            if match:
                symbols.append(match.group(1))
                continue
            js_match = re.match(r"(?:export\s+)?(?:function|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)", code)
            if js_match:
                symbols.append(js_match.group(1))
        return sorted(dict.fromkeys(symbols))

    def _check_line(self, report: dict[str, Any], marker: str) -> str:
        if not report:
            return "данных нет"
        text = str(report.get("parsed_output") or report.get("result") or "")
        lines = [line.strip() for line in text.splitlines() if marker.lower() in line.lower()]
        if lines:
            return self._short("; ".join(lines), limit=220)
        status = str(report.get("status") or "").strip()
        return status or "данных нет"

    def _scope_summary(self, reports: list[dict[str, Any]]) -> str:
        if not reports:
            return "данных нет"
        chunks = []
        for report in reports[-4:]:
            agent = report.get("agent_name") or report.get("agent") or "agent"
            chunks.append(f"{agent}: {report.get('scope_policy_result')}")
        return "; ".join(chunks)

    def _contract_summary(self, reports: list[dict[str, Any]]) -> str:
        if not reports:
            return "данных нет"
        chunks = []
        for report in reports[-4:]:
            agent = report.get("agent_name") or report.get("agent") or "agent"
            chunks.append(
                f"{agent}: completeness={report.get('contract_completeness', 'n/a')}, compliance={report.get('contract_compliance', 'n/a')}"
            )
        return "; ".join(chunks)

    def _rollback_summary(self) -> str:
        log_path = self.run_dir.parent / f"workflow_{self.run_id.replace('run_', '')}.log"
        text = self._read_text(log_path)
        lines = [line.strip() for line in text.splitlines() if "rollback" in line.lower()]
        return self._short("; ".join(lines[-5:]), limit=300) if lines else ""

    def _feedback_summary(self) -> str:
        if not self.feedback_files:
            return "нет"
        chunks = []
        for path in self.feedback_files[:6]:
            chunks.append(f"{path}: {self._short(self._read_text(path), limit=120)}")
        return "; ".join(chunks)

    def _feedback_files(self) -> list[Path]:
        if not self.feedback_root:
            return []
        run_feedback = self.feedback_root / self.project_id / self.run_id
        if not run_feedback.exists():
            return []
        return sorted(run_feedback.glob("attempt_*/*.md"))

    def _run_git(self, args: list[str]) -> str:
        if not self.target_workspace:
            return ""
        try:
            process = subprocess.run(
                ["git", *args],
                cwd=self.target_workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
            )
        except Exception:
            return ""
        if process.returncode != 0:
            return ""
        return process.stdout

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _bullet_values(values: list[str]) -> list[str]:
        if not values:
            return ["  - нет данных"]
        return [f"  - {value}" for value in values]

    @staticmethod
    def _short(value: Any, *, limit: int = 180) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return "нет данных"
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."
