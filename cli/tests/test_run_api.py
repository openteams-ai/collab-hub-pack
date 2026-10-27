"""The rest of the run API from the terminal: `run submit`, `run watch` over the stream, `run payload`, `run decide`
and `run retry`."""

from __future__ import annotations

import json

import pytest

from collab_hub_cli import main

from .conftest import HUB

OP = """
name: review
steps:
  - name: draft
    cog: echo
    entry_point: ask
    input: {prompt: "hi"}
    gate: {escalate: always}
    frames: [frame-a]
  - {name: publish, cog: echo, entry_point: run, gate: never}
"""


@pytest.fixture(autouse=True)
def dev_hub(stub, monkeypatch):
    stub.dev_auth = True
    stub.frames = {"frame-a"}
    monkeypatch.setattr(main, "POLL_SECONDS", 0)
    return stub


def test_run_submit_sends_an_op_from_a_file_with_its_gates_and_frames(stub, cli, tmp_path):
    op = tmp_path / "review.yaml"
    op.write_text(OP)
    result = cli("--hub", HUB, "run", "submit", "--file", str(op))
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "run-000000000001"
    assert "Submitted run-000000000001 (review): draft, publish" in result.stderr
    [request] = [r for r in stub.requests if r.url.path == "/v1/runs"]
    assert json.loads(request.content) == {"name": "review", "steps": [
        {"name": "draft", "cog": "echo", "entry_point": "ask", "input": {"prompt": "hi"},
         "gate": {"escalate": "always"}, "frames": ["frame-a"]},
        {"name": "publish", "cog": "echo", "entry_point": "run", "input": None, "gate": {"escalate": "never"}}]}


def test_run_submit_reads_json_from_stdin_and_a_frame_the_hub_does_not_have_is_named(stub, cli):
    result = cli("--hub", HUB, "run", "submit", "--file", "-", "--json",
                 input=json.dumps({"steps": [{"name": "a", "cog": "echo", "frames": ["frame-zzz"]}]}))
    assert result.exit_code == 1 and "No Frame frame-zzz, named by step a" in result.stderr
    result = cli("--hub", HUB, "run", "submit", "--file", "-", "--json",
                 input=json.dumps({"steps": [{"name": "a", "cog": "echo"}]}))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == "run-000000000001"
    assert cli("--hub", HUB, "run", "submit", "--file", "-", input="steps: nope").exit_code == 2


def test_run_watch_prints_the_events_as_the_hub_streams_them_and_exits_with_the_outcome(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.events["run-000000000001"] = [
        ("run_picked_up", {"controller": "dev"}), ("step_started", {"step": "echo", "attempt": 0}),
        ("step_completed", {"step": "echo", "attempt": 0}), ("completed", {})]
    stub.progress["run-000000000001"] = ["COMPLETED"]
    result = cli("--hub", HUB, "run", "watch", "run-000000000001")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["1", "08:00:01", "run_picked_up", "controller=dev"]
    assert lines[1].split() == ["2", "08:00:01", "step_started", "step=echo", "attempt=0"]
    assert "status     COMPLETED" in result.stdout


def test_run_watch_resumes_a_dropped_stream_from_the_last_event_seen(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.events["run-000000000001"] = [
        ("run_picked_up", {"controller": "dev"}), ("step_started", {"step": "echo"}), ("completed", {})]
    stub.progress["run-000000000001"] = ["COMPLETED"]
    stub.drop_stream_after = 0  # the first connection carries one event and dies mid-way through the second
    result = cli("--hub", HUB, "run", "watch", "run-000000000001", "--json")
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{\"sequence\"")]
    assert [e["sequence"] for e in events] == [1, 2, 3]  # nothing missed, nothing repeated
    streams = [r for r in stub.requests if r.url.path.endswith("/events")]
    assert [r.headers.get("Last-Event-ID") for r in streams] == [None, "1"]


def test_run_watch_stops_at_a_gate_and_exits_4(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.events["run-000000000001"] = [("step_started", {"step": "echo"}),
                                       ("gate_escalated", {"step": "echo", "escalation": "esc-1", "reason": "always"})]
    stub.progress["run-000000000001"] = ["WAITING_AT_GATE"]
    result = cli("--hub", HUB, "run", "watch", "run-000000000001")
    assert result.exit_code == 4 and "gate_escalated" in result.stdout and "WAITING_AT_GATE" in result.stdout


def test_run_payload_prints_a_completed_steps_output_and_a_failed_steps_error(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.progress["run-000000000001"] = ["COMPLETED"]
    cli("--hub", HUB, "run", "show", "run-000000000001")
    result = cli("--hub", HUB, "run", "payload", "run-000000000001", "echo")
    assert result.exit_code == 0 and json.loads(result.stdout) == {"greeting": "hi"}
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.progress["run-000000000002"] = ["FAILED"]
    cli("--hub", HUB, "run", "show", "run-000000000002")
    result = cli("--hub", HUB, "run", "payload", "run-000000000002", "echo")
    assert result.exit_code == 1 and "step echo failed: model-unavailable no model" in result.stderr
    assert cli("--hub", HUB, "run", "payload", "run-000000000002", "nope").exit_code == 1


def test_run_decide_answers_the_open_escalation_with_the_outcome_and_findings(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.progress["run-000000000001"] = ["WAITING_AT_GATE"]
    cli("--hub", HUB, "run", "show", "run-000000000001")
    result = cli("--hub", HUB, "run", "decide", "run-000000000001", "--send-back", "--finding", "too long",
                 "--finding", '{"check": "tone", "detail": "stiff"}')
    assert result.exit_code == 0, result.output
    assert "send back of echo recorded for escalation esc-1" in result.stderr
    [(run_id, step, decision)] = stub.decisions
    assert (run_id, step) == ("run-000000000001", "echo")
    assert decision == {"escalation": "esc-1", "outcome": "send_back",
                        "findings": ["too long", {"check": "tone", "detail": "stiff"}]}
    stale = cli("--hub", HUB, "run", "decide", "run-000000000001", "--approve", "--escalation", "esc-0")
    assert stale.exit_code == 1 and "esc-1 is" in stale.stderr
    assert cli("--hub", HUB, "run", "decide", "run-000000000001").exit_code == 2
    assert cli("--hub", HUB, "run", "decide", "run-000000000001", "--approve", "--finding", "x").exit_code == 2


def test_run_decide_refuses_a_run_that_waits_on_nothing(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    result = cli("--hub", HUB, "run", "decide", "run-000000000001", "--approve")
    assert result.exit_code == 1 and "waits on no decision" in result.stderr and not stub.decisions


def test_run_retry_asks_for_an_ended_run_to_run_again_and_says_how(stub, cli):
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.progress["run-000000000001"] = ["FAILED"]
    cli("--hub", HUB, "run", "show", "run-000000000001")
    result = cli("--hub", HUB, "run", "retry", "run-000000000001")
    assert result.exit_code == 0, result.output
    assert "it ended FAILED, so the failed step runs again as a new attempt" in result.stderr
    assert result.stdout.strip() == "FAILED"
    cli("--hub", HUB, "cog", "launch", "echo")
    stub.progress["run-000000000002"] = ["COMPLETED"]
    cli("--hub", HUB, "run", "show", "run-000000000002")
    nothing = cli("--hub", HUB, "run", "retry", "run-000000000002")
    assert nothing.exit_code == 1 and "there is nothing to retry" in nothing.stderr
