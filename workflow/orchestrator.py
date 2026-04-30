from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import git
import yaml

from workflow.logger import WorkflowLogger


class WorkflowOrchestrator:
    def __init__(self, config_path: str = "workflow/config.yaml") -> None:
        self.config = self._load_config(config_path)
        self.logger = WorkflowLogger()
        self.repo = git.Repo(".", search_parent_directories=True)
        self.current_branch: str | None = None
        self.task_counter = 0

    def run_full_cycle(self) -> bool:
        self.logger.info("Starting agents-pipeline workflow")
        try:
            if not self.run_research_phase():
                return False
            return self.run_implementation_phase()
        finally:
            summary = self.logger.save_summary()
            self.logger.info(f"Saved summary to {summary}")

    def run_research_phase(self) -> bool:
        phase = self.config["phases"]["research"]
        self.logger.phase_start(phase["name"])
        ok = self._run_phase_agents(phase, "research")
        if not ok:
            self.logger.phase_end(phase["name"], "failed")
            return False
        if phase.get("requires_approval") and not self._wait_for_user("Approve research results?"):
            self.logger.phase_end(phase["name"], "rejected")
            return False
        self.logger.phase_end(phase["name"], "success")
        return True

    def run_implementation_phase(self) -> bool:
        phase = self.config["phases"]["implementation"]
        self.logger.phase_start(phase["name"])
        self.task_counter += 1
        task_id = self.task_counter

        if self.config["git"]["enabled"]:
            if not self._create_git_branch(task_id):
                self.logger.phase_end(phase["name"], "failed")
                return False

        max_retries = next(
            (agent.get("max_retries", 3) for agent in phase["agents"] if agent["name"] == "developer"),
            3,
        )
        for attempt in range(1, max_retries + 1):
            self.logger.info(f"Implementation attempt {attempt}/{max_retries}")
            ok = self._run_phase_agents(phase, "implementation")
            if ok:
                if self.config["git"]["enabled"] and not self._merge_git():
                    self.logger.phase_end(phase["name"], "failed")
                    return False
                self.logger.phase_end(phase["name"], "success")
                return True

            self._save_feedback(task_id, "qa", f"Attempt {attempt} failed. Review logs and fix regressions.")
            if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                self._rollback_git(f"attempt {attempt} failed")
                if attempt < max_retries:
                    self._create_git_branch(task_id)

        self.logger.phase_end(phase["name"], "failed")
        return False

    def _run_phase_agents(self, phase: dict[str, Any], phase_key: str) -> bool:
        for agent in phase["agents"]:
            if not self._wait_for_user(f"Run {agent['name']}?"):
                self.logger.warning(f"Skipped agent {agent['name']}")
                continue
            if not self._run_agent(agent, phase_key):
                return False
        return True

    def _run_agent(self, agent_config: dict[str, Any], phase: str) -> bool:
        agent_name = agent_config["name"]
        timeout = agent_config.get("timeout", 600)
        agent_dir = Path(".openclaw/agents") / phase / agent_name

        self.logger.agent_start(agent_name, agent_config.get("description", ""))
        if not agent_dir.exists():
            self.logger.error(f"Agent directory not found: {agent_dir}")
            self.logger.agent_end(agent_name, "failed", "missing agent directory")
            return False

        cmd = ["openclaw", "run", agent_name, "--agent-dir", str(agent_dir), "--workspace", "."]
        self.logger.agent_progress(agent_name, " ".join(cmd))
        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except FileNotFoundError:
            self.logger.error("openclaw command not found", "Install openclaw or adjust PATH")
            self.logger.agent_end(agent_name, "failed", "openclaw missing")
            return False
        except subprocess.TimeoutExpired:
            self.logger.error(f"Agent timed out: {agent_name}", f"timeout={timeout}")
            self.logger.agent_end(agent_name, "failed", "timeout")
            return False

        if process.stdout:
            for line in process.stdout.splitlines():
                self.logger.agent_progress(agent_name, line)
        if process.returncode != 0:
            self.logger.error(f"Agent failed: {agent_name}", process.stderr.strip())
            self.logger.agent_end(agent_name, "failed", process.stderr.strip())
            return False

        self.logger.agent_end(agent_name, "success", "completed")
        return True

    def _wait_for_user(self, prompt: str) -> bool:
        mode = self.config["workflow"]["mode"]
        if mode == "auto":
            delay = self.config["workflow"].get("auto_continue_delay", 3)
            self.logger.info(f"Auto-continue in {delay}s: {prompt}")
            time.sleep(delay)
            return True

        answer = input(f"{prompt} [y/n/auto]: ").strip().lower()
        if answer == "auto":
            self.config["workflow"]["mode"] = "auto"
            return True
        return answer in {"y", "yes", ""}

    def _create_git_branch(self, task_id: int) -> bool:
        branch_name = f"{self.config['git']['branch_prefix']}task_{task_id}"
        try:
            self.logger.git_operation("create-branch", branch_name)
            if self.repo.is_dirty(untracked_files=True):
                self.repo.git.add(A=True)
                self.repo.index.commit(f"Auto-commit before {branch_name}")
            self.repo.git.checkout("-b", branch_name)
            self.current_branch = branch_name
            return True
        except Exception as exc:  # pragma: no cover - Git state depends on environment
            self.logger.error("Failed to create branch", str(exc))
            return False

    def _merge_git(self) -> bool:
        if not self.current_branch:
            return True
        try:
            self.logger.git_operation("merge", self.current_branch)
            self.repo.git.checkout("main")
            self.repo.git.merge(self.current_branch)
            self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Failed to merge branch", str(exc))
            return False

    def _rollback_git(self, reason: str = "") -> bool:
        try:
            self.logger.git_operation("rollback", reason)
            self.repo.git.checkout("main")
            if self.current_branch:
                self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Failed to rollback branch", str(exc))
            return False

    @staticmethod
    def _load_config(config_path: str) -> dict[str, Any]:
        with Path(config_path).open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def _save_feedback(self, task_id: int, agent: str, feedback: str) -> Path:
        feedback_dir = Path(".openclaw/feedback")
        feedback_dir.mkdir(parents=True, exist_ok=True)
        feedback_file = feedback_dir / f"task_{task_id}_{agent}.md"
        feedback_file.write_text(
            "\n".join(
                [
                    f"# Feedback from {agent}",
                    "",
                    feedback,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.logger.info(f"Saved feedback: {feedback_file}")
        return feedback_file

