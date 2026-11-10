"""The adapter conformance suite: every adapter, through the same ``Harness``, behaves the same at the seam.

Parametrized over the ACP adapter against the fake agent (with and without
embedded context) and the OpenAI-compatible adapter against the fake model.
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import fake_agent_command

from collab_hub_cog_worker import (
    AcpHarnessAdapter,
    Binding,
    Cancelled,
    Context,
    Frame,
    Harness,
    OpenAICompatAdapter,
    Prompt,
    Signal,
    WorkerError,
)

FRAMES = (Frame(id="f1", title="House style", text="Write plainly."),
          Frame(id="f2", title="Glossary", uri="frame:f2", text="A Cog is a worker."))


@pytest.fixture(params=["acp", "acp-embedded", "openai"])
def harness(request, fake_model_url):
    """A started harness of each kind, over a binding with a context window the tests can exceed."""
    binding = Binding(base_url=fake_model_url, model="fake-model", api_key="secret-key-xyz", context_window=200,
                      max_output_tokens=20)
    context = Context(binding=binding, cog="test", run_id="r")
    if request.param == "openai":
        adapter = OpenAICompatAdapter()
    else:
        flags = ("--embedded-context",) if request.param == "acp-embedded" else ()
        adapter = AcpHarnessAdapter(fake_agent_command(*flags))
    adapter.start(context)
    harness = Harness(adapter, context, FRAMES, Signal())
    yield harness
    adapter.close()


def test_a_prompt_is_answered_with_what_the_harness_said(harness):
    answer = harness.interact("hello there", frames=())
    assert "hello there" in answer.text and answer.raw
    assert harness.usage.report()["tokens"] > 0 and harness.usage.report()["interactions"] == 1


def test_the_steps_frames_reach_the_first_prompt_as_the_harness_takes_them(harness):
    answer = harness.interact("summarize").text
    if isinstance(harness.adapter, AcpHarnessAdapter):
        assert "Write plainly." in answer and "A Cog is a worker." in answer
        # An agent that takes embedded context gets each Frame as a resource with its URI; one that does not
        # gets them as text. Either way the text arrived.
        embedded = harness.adapter.capabilities["promptCapabilities"]["embeddedContext"]
        assert ("[resource frame:f2:" in answer) is embedded
    else:
        assert "[frames: House style (frame:f1), Glossary (frame:f2)]" in answer  # the fake names what it was given
    # The second interaction carries no Frames unless asked to.
    assert "Write plainly." not in harness.interact("again").text and "frames:" not in harness.interact("again").text


def test_an_input_that_cannot_fit_the_bound_model_is_context_exhausted_before_the_harness_is_asked(harness):
    with pytest.raises(WorkerError) as refused:
        harness.interact("x" * 2000, frames=())
    assert refused.value.code == "context-exhausted" and "context window of 200" in refused.value.detail
    assert harness.usage.report() is None  # nothing was spent


def test_the_harnesss_own_overflow_is_context_exhausted_too(harness):
    with pytest.raises(WorkerError) as refused:
        harness.interact("overflow", frames=())
    assert refused.value.code == "context-exhausted"


def test_a_failure_the_harness_signals_fails_the_interaction_with_a_code(harness):
    if isinstance(harness.adapter, OpenAICompatAdapter):
        pytest.skip("the fake model has no failing prompt; its failures are HTTP ones, tested below")
    with pytest.raises(WorkerError) as refused:
        harness.interact("fail", frames=())
    assert refused.value.code == "model-call-failed" and "could not answer" in refused.value.detail
    with pytest.raises(WorkerError) as silent:
        harness.interact("silence", frames=())
    assert silent.value.code == "model-response-malformed" and "end_turn" in silent.value.detail


def test_the_envelopes_binding_record_names_the_model_and_never_its_key(harness):
    record = harness.binding.record()
    assert record["model"] == "fake-model" and record["context_window"] == 200
    assert "secret-key-xyz" not in str(record) and "api_key" not in record


def test_a_cancelled_interaction_raises_cancelled_and_the_harness_still_answers_after(harness):
    if isinstance(harness.adapter, OpenAICompatAdapter):
        pytest.skip("a direct model call is not interruptible; the signal is read around it")
    threading.Timer(0.3, harness.cancel.set).start()
    with pytest.raises(Cancelled):
        harness.interact("slow", frames=())
    assert harness.alive()


def test_an_agent_that_exits_during_a_prompt_fails_it_and_is_no_longer_alive():
    adapter = AcpHarnessAdapter(fake_agent_command())
    context = Context(binding=Binding())
    adapter.start(context)
    with pytest.raises(WorkerError) as refused:
        adapter.interact(Prompt("exit"), context, Signal())
    assert refused.value.code == "model-call-failed" and "exited (code 3)" in refused.value.detail
    deadline = time.monotonic() + 5
    while adapter.alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not adapter.alive()
    adapter.close()


def test_a_permission_the_agent_asks_for_is_granted_only_for_a_declared_tool():
    context = Context(binding=Binding())
    refusing = AcpHarnessAdapter(fake_agent_command())
    refusing.start(context)
    assert refusing.interact(Prompt("use a tool"), context, Signal()).text == "permission: cancelled"
    refusing.close()
    by_title = AcpHarnessAdapter(fake_agent_command(), allowed_tools=("run a command",))  # the agent's own words
    by_title.start(context)
    assert by_title.interact(Prompt("use a tool"), context, Signal()).text == "permission: cancelled"
    by_title.close()
    allowing = AcpHarnessAdapter(fake_agent_command(), allowed_tools=("execute",))
    allowing.start(context)
    assert allowing.interact(Prompt("use a tool"), context, Signal()).text == "permission: selected allow"
    allowing.close()


def test_the_agent_inherits_only_what_it_needs_from_the_workers_environment(monkeypatch):
    import json

    monkeypatch.setenv("COLLAB_RUN_TOKEN", "secret")
    monkeypatch.setenv("COLLAB_MODEL_API_KEY", "k")
    adapter = AcpHarnessAdapter(fake_agent_command())
    context = Context(binding=Binding())
    adapter.start(context)
    seen = json.loads(adapter.interact(Prompt("env"), context, Signal()).text)
    adapter.close()
    assert seen["run_token"] is False and seen["api_key_env"] is False


def test_an_agent_that_cannot_be_started_is_model_unavailable():
    adapter = AcpHarnessAdapter(["/no/such/agent"])
    with pytest.raises(WorkerError) as refused:
        adapter.start(Context(binding=Binding()))
    assert refused.value.code == "model-unavailable"


def test_the_openai_adapter_refuses_to_start_without_an_endpoint_and_reports_an_unreachable_one():
    with pytest.raises(WorkerError) as unbound:
        OpenAICompatAdapter().start(Context(binding=Binding()))
    assert unbound.value.code == "binding-invalid"
    context = Context(binding=Binding(base_url="http://127.0.0.1:9/v1", model="m"))
    adapter = OpenAICompatAdapter(timeout=2)
    adapter.start(context)
    with pytest.raises(WorkerError) as unreachable:
        adapter.interact(Prompt("hi"), context, Signal())
    assert unreachable.value.code == "model-unavailable"


def test_an_agent_that_exits_before_answering_initialize_is_model_unavailable_with_its_exit_code():
    adapter = AcpHarnessAdapter(fake_agent_command("--exit-on-initialize"))
    with pytest.raises(WorkerError) as refused:
        adapter.start(Context(binding=Binding()))
    assert refused.value.code == "model-unavailable"
    assert "exited (code 4) before answering initialize" in refused.value.detail
    assert not adapter.alive()


def test_a_rate_limit_is_not_a_context_overflow():
    from collab_hub_cog_worker.limits import looks_like_overflow

    assert not looks_like_overflow("Rate limit reached for model x: Limit 30000 tokens per min, requested 31000")
    assert not looks_like_overflow("You exceeded your current quota")
    assert looks_like_overflow("context_length_exceeded: the prompt is too long")
    assert looks_like_overflow("This model's maximum context length is 8192 tokens")
    assert looks_like_overflow("prompt is too long: 120000 tokens > 100000 maximum")
