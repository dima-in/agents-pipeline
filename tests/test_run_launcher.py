import run_launcher
from run_launcher import format_normalized_args, normalize_cli_args


def test_phase_alias_research_normalizes_to_phase_flag() -> None:
    assert normalize_cli_args(["research"]) == ["--phase", "research"]
    assert normalize_cli_args(["r", "--log-level", "DEBUG"]) == ["--phase", "research", "--log-level", "DEBUG"]


def test_phase_aliases_preserve_following_flags() -> None:
    assert normalize_cli_args(["impl", "--mode", "interactive"]) == [
        "--phase",
        "implementation",
        "--mode",
        "interactive",
    ]
    assert normalize_cli_args(["deploy", "--skip-git"]) == ["--phase", "deployment", "--skip-git"]


def test_mode_aliases_normalize_to_mode_flag() -> None:
    assert normalize_cli_args(["auto"]) == ["--mode", "auto"]
    assert normalize_cli_args(["interactive", "--phase", "research"]) == ["--mode", "interactive", "--phase", "research"]


def test_existing_start_args_are_preserved() -> None:
    assert normalize_cli_args(["--phase", "research"]) == ["--phase", "research"]
    assert normalize_cli_args(["python", "manage_agents.py", "list"]) == ["python", "manage_agents.py", "list"]


def test_format_normalized_args_quotes_paths_with_spaces() -> None:
    assert (
        format_normalized_args(["--workspace", r"C:\My Project", "--phase", "research"])
        == '--workspace "C:\\My Project" --phase research'
    )


def test_repo_map_subcommand_dispatches_to_repo_map_tool(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_repo_map_main(argv):
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(run_launcher.repo_map_tool, "main", fake_repo_map_main)
    monkeypatch.setattr(run_launcher.start.os, "getcwd", lambda: r"C:\work\agents-pipeline")
    monkeypatch.delenv("AGENTS_PIPELINE_LAUNCH_CWD", raising=False)

    exit_code = run_launcher.main(["repo-map"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "--workspace" in captured["argv"]
    assert "--project-id" in captured["argv"]
    assert "normalized_args=python tools/repo_map.py" in output
