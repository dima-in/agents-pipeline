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
