"""The Hermes harness Cog (`cogs/hermes`): Hermes Agent behind the seam, driven over ACP.

Most of these run the Cog's real worker against a stand-in ACP agent, so they
need neither Hermes's 400 MB environment nor a model. The last runs Hermes
itself against the fake model, where the Cog's pixi environment is installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from collab_hub_execution import InMemoryTrackStore, LifecycleRunner, OpDefinition, OpStep, RunState, intents
from collab_hub_execution.controller import RunController

REPOSITORY = Path(__file__).resolve().parents[2]
HERMES = REPOSITORY / "cogs" / "hermes"
FAKE_AGENT = Path(__file__).resolve().parent / "fake_acp_agent.py"
FAKE_MODEL = REPOSITORY / "dev" / "fake-model" / "fake_model.py"
BY = {"user": "alice", "org_id": "acme"}


def _controller(tmp_path, track, environment):
    """A controller over a copy of the Hermes Cog that runs under this Python, against a stand-in agent."""
    root = tmp_path / "cogs"
    shutil.copytree(HERMES, root / "hermes", ignore=shutil.ignore_patterns(".pixi"))
    manifest = root / "hermes" / "pixi.toml"
    manifest.write_text(re.sub(r'^serve = .*$', f'serve = "{sys.executable} serve.py"', manifest.read_text(),
                               flags=re.M))
    environment = {"COLLAB_HERMES_COMMAND": f"{sys.executable} {FAKE_AGENT}", **environment}
    runner = LifecycleRunner(track=track, location="local", location_settings={
        "packages": [root], "work_dir": tmp_path / "runs", "environment": "host", "interaction_timeout": None,
        "deliver": lambda cog, run_id, instance: environment if cog == "hermes" else {}})
    return RunController(runner, poll_interval=0.01)


def _until(controller, check, what, seconds=60):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, what
        controller.tick()
        time.sleep(0.02)


def _answer(controller, track, run_id, text):
    turn = intents.request_turn(track, run_id, text=text, actor="alice")
    _until(controller, lambda: intents.turns(track.replay(run_id))[turn.turn].state != "pending", f"no answer: {text}")
    view = intents.turns(track.replay(run_id))[turn.turn]
    assert view.state == "answered", view.error
    return view.answer


MODEL = {"COLLAB_MODEL_BASE_URL": "http://127.0.0.1:9/v1", "COLLAB_MODEL_NAME": "a-model",
         "COLLAB_MODEL_API_KEY": "the-key"}


def test_a_hermes_session_answers_each_turn_with_what_the_agent_said(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, MODEL)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    # Asked before the session is open: the worker waits for it rather than refusing the turn.
    assert _answer(controller, track, "r", "hello") == "agent heard: hello"
    assert _answer(controller, track, "r", "and again") == "agent heard: and again"
    assert _answer(controller, track, "r", "bye").startswith("Bye!")
    _until(controller, lambda: intents.describe(track, "r").state is RunState.COMPLETED, "the session did not end")
    assert intents.describe(track, "r").steps[0].output == {"turns": 2}


def test_hermes_gets_the_delivered_model_in_a_home_of_its_own_and_never_the_secrets_in_its_environment(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, MODEL)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    seen = json.loads(_answer(controller, track, "r", "env"))
    # Chat completions whatever the URL's host, and nothing installed while it runs.
    assert seen["model"] == {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "default": "a-model",
                             "api_key": "the-key", "api_mode": "chat_completions"}
    assert seen["security"] == {"allow_lazy_installs": False}
    assert seen["run_token"] is False and seen["api_key_env"] is False and seen["anthropic_key"] is None
    assert Path(seen["cwd"]).name.startswith("hermes-r-")  # a workspace of the run's, not the package
    assert seen["home"] == seen["cwd"]  # so no credentials of the machine's own are found under HOME
    intents.request_cancel(track, "r", actor="alice")
    _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "not cancelled")
    # Terminated, the worker removes the session's workspace: its config names the model and its key.
    deadline = time.monotonic() + 10
    while Path(seen["cwd"]).exists():
        assert time.monotonic() < deadline, "the session's workspace outlived the run"
        time.sleep(0.05)


def test_with_claude_hermes_uses_its_anthropic_provider_and_gets_only_that_key(tmp_path):
    # A provider key the worker happens to have, here Gemini's, never reaches Hermes: it would use it.
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, {"COLLAB_MODEL_PROVIDER": "anthropic", "COLLAB_MODEL_API_KEY": "sk-key",
                                               "GEMINI_API_KEY": "not-for-hermes"})
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    seen = json.loads(_answer(controller, track, "r", "env"))
    assert seen["model"] == {"provider": "anthropic", "default": "claude-opus-5-5"}  # no key written to disk
    assert seen["anthropic_key"] == "sk-key" and seen["gemini_key"] is False
    intents.request_cancel(track, "r", actor="alice")
    _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "not cancelled")


def test_a_delivered_ca_bundle_reaches_hermes(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, {**MODEL, "SSL_CERT_FILE": "/delivered/ca.pem",
                                               "REQUESTS_CA_BUNDLE": "/delivered/ca.pem"})
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    seen = json.loads(_answer(controller, track, "r", "env"))
    assert seen["ssl_cert_file"] == "/delivered/ca.pem" and seen["requests_ca_bundle"] == "/delivered/ca.pem"
    intents.request_cancel(track, "r", actor="alice")
    _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "not cancelled")


def _serve_module():
    """The Hermes Cog's worker module, imported from the package: its environment assembly is tested in-process."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("hermes_serve", HERMES / "serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hermes_gets_its_environments_ca_bundle_only_inside_pixi_and_never_over_a_delivered_one(tmp_path, monkeypatch):
    serve = _serve_module()
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("")
    monkeypatch.setattr(serve, "CA_BUNDLE", bundle)
    for name in ("PIXI_ENVIRONMENT_NAME", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    # The `host` environment: whatever interpreter runs the worker, whose bundle is not the package's.
    assert not {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"} & serve.hermes_environment().keys()
    # The package's pixi environment: its conda bundle, current by the lock, for httpx and requests alike.
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    env = serve.hermes_environment()
    assert env["SSL_CERT_FILE"] == str(bundle) and env["REQUESTS_CA_BUNDLE"] == str(bundle)
    # Delivered bundles win, and a delivered directory alone disables the defaults too: Hermes resolves
    # SSL_CERT_FILE or REQUESTS_CA_BUNDLE itself and otherwise lets httpx read SSL_CERT_FILE, then SSL_CERT_DIR,
    # so a default file beside the delivered directory would take precedence over it.
    monkeypatch.setenv("SSL_CERT_DIR", "/delivered/ca")
    env = serve.hermes_environment()
    assert env["SSL_CERT_DIR"] == "/delivered/ca"
    assert not {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"} & env.keys()
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/delivered/ca.pem")
    env = serve.hermes_environment()
    assert "SSL_CERT_FILE" not in env
    assert env["SSL_CERT_DIR"] == "/delivered/ca" and env["REQUESTS_CA_BUNDLE"] == "/delivered/ca.pem"
    # An environment without the bundle sets nothing either.
    monkeypatch.delenv("SSL_CERT_DIR")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    bundle.unlink()
    assert not {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"} & serve.hermes_environment().keys()


@pytest.mark.parametrize(("environment", "why"), [
    ({"COLLAB_MODEL_PROVIDER": "anthropic"}, "needs COLLAB_MODEL_API_KEY"),
    ({"COLLAB_MODEL_PROVIDER": "gemini", "COLLAB_MODEL_BASE_URL": "http://x/v1"}, "unknown COLLAB_MODEL_PROVIDER"),
])
def test_a_model_the_worker_cannot_use_fails_the_step_and_says_why(tmp_path, environment, why):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, environment)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    _until(controller, lambda: intents.describe(track, "r").state.ended, "the run did not end")
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "model-unavailable" and why in view.reason


def test_a_tool_hermes_asks_permission_for_is_refused(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, MODEL)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    assert _answer(controller, track, "r", "use a tool") == "permission: cancelled"
    intents.request_cancel(track, "r", actor="alice")
    _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "not cancelled")


def test_a_run_stopped_while_hermes_is_still_starting_leaves_no_workspace(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, {**MODEL, "COLLAB_HERMES_COMMAND":
                                               f"{sys.executable} {FAKE_AGENT} --slow-start"})
    intents.submit(track, OpDefinition("slowstart", (OpStep("chat", "hermes", "session"),)), by=BY)
    workspaces = lambda: list(Path(tempfile.gettempdir()).glob("hermes-slowstart-*"))  # noqa: E731
    _until(controller, lambda: workspaces(), "the session never began opening")
    [workspace] = workspaces()
    intents.request_cancel(track, "slowstart", actor="alice")
    _until(controller, lambda: intents.describe(track, "slowstart").state is RunState.CANCELLED, "not cancelled")
    deadline = time.monotonic() + 10
    while workspace.exists():
        assert time.monotonic() < deadline, "a session stopped while opening left its workspace"
        time.sleep(0.05)


def test_a_session_whose_hermes_exits_ends_failed_and_leaves_nothing(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, MODEL)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    workspace = Path(json.loads(_answer(controller, track, "r", "env"))["cwd"])
    turn = intents.request_turn(track, "r", text="exit", actor="alice")  # the stand-in exits on this
    _until(controller, lambda: intents.describe(track, "r").state.ended, "the run stayed open without its Hermes")
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "model-call-failed" and "Hermes exited" in view.reason
    assert intents.turns(track.replay("r"))[turn.turn].state == "failed"
    assert not workspace.exists()


def test_ask_answers_one_prompt(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, MODEL)
    intents.submit(track, OpDefinition("r", (OpStep("ask", "hermes", "ask", {"prompt": "one question"}),)), by=BY)
    _until(controller, lambda: intents.describe(track, "r").state.ended, "the run did not end")
    view = intents.describe(track, "r")
    assert view.state is RunState.COMPLETED and view.steps[0].output == {"answer": "agent heard: one question"}


def test_without_a_model_the_step_fails_and_says_why(tmp_path):
    track = InMemoryTrackStore()
    controller = _controller(tmp_path, track, {})
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    _until(controller, lambda: intents.describe(track, "r").state.ended, "the run did not end")
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "model-unavailable"
    assert "COLLAB_MODEL_BASE_URL" in view.reason


NEEDS_HERMES = pytest.mark.skipif(
    not (HERMES / ".pixi" / "envs" / "default").is_dir() or shutil.which("pixi") is None,
    reason="the Hermes Cog's pixi environment is not installed (make -C examples/cog-local env)")


def _hermes_session(tmp_path, model_command, port):
    """Hermes itself, in its own environment, in a session, against a model this test runs."""
    model = subprocess.Popen([sys.executable, *model_command], stderr=open(tmp_path / "model.log", "w"))
    track = InMemoryTrackStore()
    runner = LifecycleRunner(track=track, location="local", location_settings={
        "packages": [REPOSITORY / "cogs"], "allow": ["hermes"], "work_dir": tmp_path / "runs",
        "interaction_timeout": None,
        "deliver": lambda cog, run_id, instance: {"COLLAB_MODEL_BASE_URL": f"http://127.0.0.1:{port}/v1",
                                                  "COLLAB_MODEL_NAME": "fake-model"}})
    controller = RunController(runner, poll_interval=0.02)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    return model, track, controller


def _end(model, track, controller):
    try:
        intents.request_cancel(track, "r", actor="alice")
        _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "not cancelled")
    finally:
        model.terminate()
        model.wait(timeout=10)


@NEEDS_HERMES
def test_hermes_itself_answers_through_the_fake_model(tmp_path):
    model, track, controller = _hermes_session(tmp_path, [str(FAKE_MODEL), "--port", "18791"], 18791)
    try:
        assert _answer(controller, track, "r", "hello hermes") == "The fake model heard: hello hermes"
    finally:
        _end(model, track, controller)


@NEEDS_HERMES
def test_hermes_runs_no_command_even_when_its_model_orders_one(tmp_path):
    # Decision 14: prompt in, answer out. The model asks for the `terminal` tool; Hermes has none to run.
    marker = tmp_path / "hermes-ran-a-command"
    ordering = Path(__file__).resolve().parent / "tool_ordering_model.py"
    model, track, controller = _hermes_session(tmp_path, [str(ordering), "18792", str(marker)], 18792)
    try:
        answer = _answer(controller, track, "r", "please run a command")
    finally:
        _end(model, track, controller)
    assert not marker.exists(), "Hermes ran the command its model ordered"
    assert "Tool 'terminal' does not exist" in answer
    assert "offered: []" in (tmp_path / "model.log").read_text()  # no tool was even offered to the model


BEDROCK_PROBE = """
import os, subprocess, sys, tempfile
sys.path.insert(0, os.getcwd())
import serve
root = tempfile.mkdtemp()
os.environ.update(HERMES_HOME=str(serve.hermes_home(root)), HOME=root)
installs = []
run = subprocess.run
subprocess.run = lambda *a, **k: (installs.append(str(a[0] if a else k.get("args"))), run(*a, **k))[1]
from hermes_cli.runtime_provider import resolve_runtime_provider
from run_agent import AIAgent
from tools import lazy_deps
rt = resolve_runtime_provider()
agent = AIAgent(base_url=rt["base_url"], api_key=rt["api_key"], provider=rt["provider"], api_mode=rt["api_mode"],
                model=os.environ["COLLAB_MODEL_NAME"], quiet_mode=True, skip_context_files=True, enabled_toolsets=[])
print(rt["api_mode"], agent.api_mode, lazy_deps._allow_lazy_installs(), [c for c in installs if "pip" in c])
"""


@NEEDS_HERMES
def test_hermes_speaks_chat_completions_to_a_bedrock_url_and_installs_nothing(tmp_path):
    # Hermes 0.19 would talk to bedrock-runtime.*.amazonaws.com through Bedrock's Converse API and boto3,
    # installing boto3 into the locked environment on first use. The Cog pins chat completions, and no installs.
    environment = {**{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")},
                   "COLLAB_MODEL_PROVIDER": "openai-compatible", "COLLAB_MODEL_NAME": "openai.gpt-oss-120b-1:0",
                   "COLLAB_MODEL_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
                   "COLLAB_MODEL_API_KEY": "not-a-key"}
    probed = subprocess.run(["pixi", "run", "--locked", "--manifest-path", str(HERMES / "pixi.toml"), "python", "-c",
                             BEDROCK_PROBE], cwd=HERMES, env=environment, capture_output=True, text=True, timeout=300)
    assert probed.returncode == 0, probed.stderr[-2000:]
    assert probed.stdout.strip().splitlines()[-1] == "chat_completions chat_completions False []"


@NEEDS_HERMES
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("COLLAB_TEST_CLAUDE") != "1",
                    reason="spends a few Claude tokens: set COLLAB_TEST_CLAUDE=1 and ANTHROPIC_API_KEY to run it")
def test_hermes_chats_with_claude_for_real(tmp_path):
    track = InMemoryTrackStore()
    key = os.environ["ANTHROPIC_API_KEY"]
    runner = LifecycleRunner(track=track, location="local", location_settings={
        "packages": [REPOSITORY / "cogs"], "allow": ["hermes"], "work_dir": tmp_path / "runs",
        "interaction_timeout": None,
        "deliver": lambda cog, run_id, instance: {"COLLAB_MODEL_PROVIDER": "anthropic", "COLLAB_MODEL_API_KEY": key}})
    controller = RunController(runner, poll_interval=0.05)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hermes", "session"),)), by=BY)
    try:
        assert "391" in _answer(controller, track, "r", "What is 17 times 23? Answer with the number only.")
    finally:
        intents.request_cancel(track, "r", actor="alice")
        _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "not cancelled")
