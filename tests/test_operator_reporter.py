import json

from workflow.operator_reporter import OperatorReporter


def make_reporter(**overrides) -> OperatorReporter:
    defaults = {
        "url": "https://example.invalid/api/pipeline/events",
        "token": "secret-token",
        "project": "github.com-dima-in-oil",
        "run_id": "run_20260910_113000",
    }
    defaults.update(overrides)
    return OperatorReporter(**defaults)


def test_event_carries_the_agreed_contract_fields() -> None:
    reporter = make_reporter()

    event = reporter.build_event(
        "blocker",
        "Задача: TASK-003\nБлокер: нарушен порядок",
        task="TASK-003",
        needs_human=True,
        options=["Запустить сначала задачу-часть", "  ", "Пропустить задачу."],
    )

    assert event == {
        "run_id": "run_20260910_113000",
        "project": "github.com-dima-in-oil",
        "task": "TASK-003",
        "kind": "blocker",
        # Newlines collapse: the chat renders one message, not a pre-formatted console box.
        "text": "Задача: TASK-003 Блокер: нарушен порядок",
        "needs_human": True,
        # Blank options are dropped — they would render as empty buttons.
        "options": ["Запустить сначала задачу-часть", "Пропустить задачу."],
    }


def test_unknown_kind_degrades_to_status() -> None:
    # A typo must not produce an event the chat cannot route.
    assert make_reporter().build_event("explosion", "x")["kind"] == "status"


def test_disabled_bridge_still_records_what_it_would_send() -> None:
    # The pipeline half must be verifiable before any chat half exists.
    reporter = make_reporter(enabled=False)

    assert reporter.send("done", "TASK-004 готов", task="TASK-004") is False
    assert reporter.sent[-1]["kind"] == "done"
    assert reporter.sent[-1]["task"] == "TASK-004"


def test_missing_url_disables_the_bridge() -> None:
    assert make_reporter(url="").enabled is False


def test_unreachable_bridge_never_raises(monkeypatch) -> None:
    # The single most important property: the chat is a side channel. A dead bridge must not
    # fail a run that is otherwise green.
    def explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("workflow.operator_reporter.urllib.request.urlopen", explode)

    assert make_reporter().send("blocker", "нужна правка движка", needs_human=True) is False


def test_successful_send_posts_json_with_bearer_token(monkeypatch) -> None:
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def capture(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("workflow.operator_reporter.urllib.request.urlopen", capture)

    assert make_reporter(timeout=3.0).send("status", "фаза началась", task="TASK-003") is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.invalid/api/pipeline/events"
    assert captured["auth"] == "Bearer secret-token"
    assert captured["timeout"] == 3.0
    assert captured["body"]["text"] == "фаза началась"


def test_from_config_reads_the_bridge_block(monkeypatch) -> None:
    monkeypatch.setenv("MY_BRIDGE_TOKEN", "from-env")
    config = {
        "workflow": {
            "operator_bridge": {
                "enabled": True,
                "url": "https://novpnai.ru/api/pipeline/events",
                "token_env": "MY_BRIDGE_TOKEN",
                "timeout_seconds": 5,
            }
        }
    }

    reporter = OperatorReporter.from_config(config, project="p", run_id="r")

    assert reporter.enabled is True
    assert reporter.token == "from-env"
    assert reporter.timeout == 5.0


def test_from_config_defaults_to_disabled() -> None:
    # Shipping enabled-by-default would post a new user's run events to nowhere on every run.
    assert OperatorReporter.from_config({}, project="p", run_id="r").enabled is False


def test_recorded_events_are_capped() -> None:
    reporter = make_reporter(enabled=False)
    for index in range(60):
        reporter.send("status", f"event {index}")

    assert len(reporter.sent) == 50
    assert reporter.sent[-1]["text"] == "event 59"
