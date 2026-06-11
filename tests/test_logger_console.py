"""Compact console mode: the operator sees handoff/result cards and statuses with
reasons; service dumps (prompts, raw responses, diagnostics) stay in the log file."""
from workflow.logger import WorkflowLogger


def _make(tmp_path, verbosity: str) -> WorkflowLogger:
    return WorkflowLogger(log_dir=str(tmp_path / "logs"), console_verbosity=verbosity)


def test_compact_console_hides_service_output(tmp_path, capsys) -> None:
    logger = _make(tmp_path, "compact")
    logger.agent_progress("code-developer", "Executor command:")
    logger.agent_progress("code-developer", "Direct API retrieval turn 1: read_file")
    logger.info("rollback_dirty_worktree=True")
    logger.info("selected_python=C:/venv/python.exe")
    logger.info("Multi developer allowed paths (code-developer): main.py")
    logger.operator_box("Prompt сохранён -> code-developer", ["Файл: report.md"])
    logger.operator_box("Ответ агента -> code-developer", ['{"tool":"write_file", ...giant json...}'])
    logger.operator_box("Feedback для retry -> code-developer", ["100 строк pytest-дампа"])
    logger.operator_box("Ожидание ответа -> code-developer", ["ждём ответ модели"])
    out = capsys.readouterr().out
    assert "Executor command" not in out
    assert "retrieval turn" not in out
    assert "rollback_dirty_worktree" not in out
    assert "selected_python" not in out
    assert "Prompt сохранён" not in out
    assert "Ответ агента" not in out
    assert "Feedback для retry" not in out


def test_compact_console_keeps_cards_status_and_reason(tmp_path, capsys) -> None:
    logger = _make(tmp_path, "compact")
    logger.agent_start("qa", "Run checks and report regressions")
    logger.operator_box("Передача: Разработчик -> QA", ["Должен: проверить результат против контракта"])
    logger.operator_box("Готово: QA", ["Сделал: вердикт: failed"])
    logger.operator_box("Human summary (RU)", ["QA нашёл регрессии."])
    logger.agent_end("qa", "qa_failed", "qa reported regressions")
    logger.info("Попытка реализации 2/3")
    out = capsys.readouterr().out
    assert "Агент запущен: qa" in out
    assert "Передача: Разработчик -> QA" in out
    assert "Готово: QA" in out
    assert "Human summary" in out
    assert "QA отклонил" in out  # qa_failed translated for the operator
    assert "qa reported regressions" in out
    assert "Попытка реализации 2/3" in out


def test_verbose_console_prints_everything(tmp_path, capsys) -> None:
    logger = _make(tmp_path, "verbose")
    logger.agent_progress("qa", "Executor command:")
    logger.operator_box("Ответ агента -> qa", ["строка ответа"])
    out = capsys.readouterr().out
    assert "Executor command" in out
    assert "Ответ агента" in out


def test_compact_keeps_full_detail_in_log_file(tmp_path) -> None:
    logger = _make(tmp_path, "compact")
    logger.operator_box("Ответ агента -> qa", ["важная деталь для отладки"])
    logger.info("rollback_dirty_worktree=True")
    text = logger.log_file.read_text(encoding="utf-8")
    assert "Ответ агента" in text
    assert "важная деталь для отладки" in text
    assert "rollback_dirty_worktree" in text


def test_failure_statuses_translated_for_operator() -> None:
    translate = WorkflowLogger._translate_status
    assert translate("template_validation_failed") == "валидатор отклонил"
    assert translate("developer_checks_failed") == "проверки кода не пройдены"
    assert translate("no_changes") == "без изменений"
    assert translate("strict_retrieval_blocked") == "превышен бюджет чтения"
    assert translate("success") == "успех"
