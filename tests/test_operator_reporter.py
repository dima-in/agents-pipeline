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


def test_reply_to_is_attached_only_when_answering_a_command() -> None:
    # The answer to a read command rides an ordinary status event tagged with the command id,
    # so neither side needs a second endpoint.
    reporter = make_reporter()

    answered = reporter.build_event("status", "дифф...", reply_to="cmd-42")
    assert answered["reply_to"] == "cmd-42"

    # An unsolicited event must not carry an empty reply_to — the chat would thread it nowhere.
    assert "reply_to" not in reporter.build_event("status", "фаза началась")
    assert "reply_to" not in reporter.build_event("status", "x", reply_to="   ")


def test_command_answers_keep_their_markdown_other_events_stay_one_line() -> None:
    # The chat renders replies as markdown, so a reply's newlines ARE its structure (a ```diff
    # fence, a task list) — and leading indentation is meaningful in a diff. A blocker or a run
    # summary is still read at a glance: one line.
    reporter = make_reporter()

    reply = reporter.build_event("status", "\n```diff\n+ added\n context  \n- removed\n```\n\n", reply_to="cmd-1")
    assert reply["text"] == "```diff\n+ added\n context\n- removed\n```"

    assert reporter.build_event("status", "line one\nline two")["text"] == "line one line two"


def test_command_answers_stay_under_the_chat_limit() -> None:
    # The chat caps an event at 8000 characters.
    reply = make_reporter().build_event("status", "x" * 20000, reply_to="cmd-1")

    assert len(reply["text"]) <= 8000


def test_commands_url_defaults_to_the_events_sibling() -> None:
    assert make_reporter().commands_url == "https://example.invalid/api/pipeline/commands"
    assert make_reporter(commands_url="https://other/cmds").commands_url == "https://other/cmds"


def test_poll_commands_returns_well_formed_commands(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "commands": [
                        {"id": "c1", "cmd": "diff", "args": {}},
                        {"id": "c2", "cmd": "", "args": {}},  # malformed: no command name
                        "not-a-dict",
                        {"id": "c3", "cmd": "answer", "args": {"choice": "Пропустить задачу."}},
                    ]
                }
            ).encode("utf-8")

    def capture(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("workflow.operator_reporter.urllib.request.urlopen", capture)

    commands = make_reporter().poll_commands(wait=25)

    assert [c["cmd"] for c in commands] == ["diff", "answer"]
    assert "project=github.com-dima-in-oil" in captured["url"] and "wait=25" in captured["url"]
    assert captured["auth"] == "Bearer secret-token"
    # The socket must outlive the server's hold, or every long poll would look like a failure.
    assert captured["timeout"] > 25


def test_poll_commands_survives_an_empty_or_broken_answer(monkeypatch) -> None:
    # An empty list is the NORMAL answer (no command issued) — it must not look like an error,
    # and a dead or garbage-returning bridge must not raise either.
    def explode(*args, **kwargs):
        raise OSError("timed out")

    monkeypatch.setattr("workflow.operator_reporter.urllib.request.urlopen", explode)
    assert make_reporter().poll_commands(wait=5) == []

    # Disabled bridge never reaches the network at all.
    assert make_reporter(enabled=False).poll_commands(wait=5) == []


def test_recorded_events_are_capped() -> None:
    reporter = make_reporter(enabled=False)
    for index in range(60):
        reporter.send("status", f"event {index}")

    assert len(reporter.sent) == 50
    assert reporter.sent[-1]["text"] == "event 59"
