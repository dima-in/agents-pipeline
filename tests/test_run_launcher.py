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
