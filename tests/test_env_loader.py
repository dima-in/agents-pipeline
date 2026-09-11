import os

from workflow.env_loader import _parse_env_text, load_env_files


def test_parse_env_text_handles_export_quotes_and_comments() -> None:
    parsed = _parse_env_text(
        "# comment line\n"
        "export OPENROUTER_API_KEY=\"sk-from-file\"\n"
        "FOO=bar\n"
        "QUOTED='q'\n"
        "no_equals_ignored\n"
        "\n"
    )
    assert parsed["OPENROUTER_API_KEY"] == "sk-from-file"
    assert parsed["FOO"] == "bar"
    assert parsed["QUOTED"] == "q"
    assert "no_equals_ignored" not in parsed


def test_load_env_files_fills_missing_but_never_overrides(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("PIPELINE_FILL=from-file\nPIPELINE_KEEP=from-file\n", encoding="utf-8")
    monkeypatch.delenv("PIPELINE_FILL", raising=False)
    monkeypatch.setenv("PIPELINE_KEEP", "from-real-env")  # a real env var must win

    try:
        loaded = load_env_files([tmp_path])
        assert loaded and str(tmp_path) in loaded[0]
        assert os.environ["PIPELINE_FILL"] == "from-file"  # missing -> filled from .env
        assert os.environ["PIPELINE_KEEP"] == "from-real-env"  # present -> NOT overridden
    finally:
        os.environ.pop("PIPELINE_FILL", None)


def test_load_env_files_missing_file_is_noop(tmp_path) -> None:
    assert load_env_files([tmp_path]) == []


def test_parse_env_text_strips_a_leading_bom() -> None:
    # Notepad's "UTF-8 with BOM" must not glue the BOM onto the first key and hide it.
    parsed = _parse_env_text("﻿OPENROUTER_API_KEY=sk-x\nFOO=bar\n")

    assert parsed["OPENROUTER_API_KEY"] == "sk-x"
    assert parsed["FOO"] == "bar"


def test_load_env_files_reads_a_file_saved_with_bom(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_bytes("﻿PIPELINE_BOM_KEY=from-file\n".encode("utf-8"))
    monkeypatch.delenv("PIPELINE_BOM_KEY", raising=False)

    try:
        load_env_files([tmp_path])
        assert os.environ["PIPELINE_BOM_KEY"] == "from-file"
    finally:
        os.environ.pop("PIPELINE_BOM_KEY", None)


def test_malformed_lines_are_reported_by_number_never_by_content() -> None:
    # The operator-bridge token was pasted as bare values — no KEY=, one with a stray "'n" from a
    # keyboard layout — and the loader skipped them silently, so the key simply "was not found".
    # Report such lines, but never echo them: a bare line IS the secret.
    secret = "3f9a1c7e5b2d4f60a8e1c3b5d7f9a2c4"
    problems: list[str] = []

    parsed = _parse_env_text(f"FOO=bar\n{secret}\n'n{secret}\n'nBAD KEY=value\n", problems)

    assert parsed == {"FOO": "bar"}
    assert [problem.split(":")[0] for problem in problems] == ["строка 2", "строка 3", "строка 4"]
    assert all(secret not in problem and "value" not in problem for problem in problems)


def test_load_env_files_reports_problems_with_the_file_path(tmp_path) -> None:
    (tmp_path / ".env").write_text("just-a-bare-token\n", encoding="utf-8")
    problems: list[str] = []

    load_env_files([tmp_path], problems=problems)

    assert len(problems) == 1
    assert str(tmp_path) in problems[0] and "строка 1" in problems[0]
    assert "just-a-bare-token" not in problems[0]
