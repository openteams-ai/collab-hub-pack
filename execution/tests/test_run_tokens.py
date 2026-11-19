"""The run token on the hub's side: its shape, the live worker it names, and the attempt it still commits for."""

from __future__ import annotations

from collab_hub_execution import InMemoryTrackStore, intents, run_tokens
from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent


def _track(*events):
    track = InMemoryTrackStore()
    for kind, payload in events:
        track.append(TrackEvent(run_id="r", event_type=kind, payload=payload, schema=SCHEMA_VERSION))
    return track


def _started(token, step="s", attempt=0):
    return "worker_started", {"step": step, "attempt": attempt, "instance": f"{step}:{attempt}",
                              "run_token_sha256": run_tokens.digest(token)}


def test_a_minted_token_is_well_formed_and_little_else_is():
    for _ in range(20):
        assert run_tokens.well_formed(run_tokens.mint())
    for text in ("", "run-token", "x" * 42, "x" * 44, "x" * 43 + "=", "é" * 43, run_tokens.mint() + "/"):
        assert not run_tokens.well_formed(text), text


def test_a_token_verifies_for_its_attempt_until_the_attempt_has_an_outcome_whether_or_not_its_worker_is_up():
    token, other = run_tokens.mint(), run_tokens.mint()
    track = _track(("op_submitted", {}), ("run_picked_up", {}), ("step_started", {"step": "s", "attempt": 0}),
                   _started(token))
    assert run_tokens.verify(track.replay("r"), token)["instance"] == "s:0"
    assert run_tokens.verify_attempt(track.replay("r"), token)["instance"] == "s:0"
    assert run_tokens.verify_attempt(track.replay("r"), other) is None
    # The controller died and its successor recorded the run interrupted and the worker stopped: the worker,
    # still acting, is no longer up, but its attempt has no outcome, so it commits.
    track.append(TrackEvent(run_id="r", event_type="interrupted", payload={"backend": "none"}, schema=1))
    track.append(TrackEvent(run_id="r", event_type="worker_stopped", payload={"step": "s", "attempt": 0,
                                                                              "instance": "s:0"}, schema=1))
    assert run_tokens.verify(track.replay("r"), token) is None
    assert run_tokens.verify_attempt(track.replay("r"), token)["run_token_sha256"] == run_tokens.digest(token)
    # The retry resumed the attempt with a new worker: each token is its own worker's, both on the one attempt.
    track.append(TrackEvent(run_id="r", event_type="retry_requested", payload={"attempt": "same"}, schema=1))
    track.append(TrackEvent(run_id="r", event_type=_started(other)[0], payload=_started(other)[1], schema=1))
    assert run_tokens.verify_attempt(track.replay("r"), token)["run_token_sha256"] == run_tokens.digest(token)
    assert run_tokens.verify_attempt(track.replay("r"), other)["run_token_sha256"] == run_tokens.digest(other)
    # The attempt's outcome is on the Track: nothing is left for either token to commit.
    track.append(TrackEvent(run_id="r", event_type="step_completed", payload={"step": "s", "attempt": 0},
                            schema=1))
    assert run_tokens.verify_attempt(track.replay("r"), token) is None
    assert run_tokens.verify_attempt(track.replay("r"), other) is None
    # Another attempt's outcome settles nothing of this one.
    late = run_tokens.mint()
    track.append(TrackEvent(run_id="r", event_type=_started(late, attempt=1)[0],
                            payload=_started(late, attempt=1)[1], schema=1))
    track.append(TrackEvent(run_id="r", event_type="step_failed", payload={"step": "s", "attempt": 0}, schema=1))
    assert run_tokens.verify_attempt(track.replay("r"), late)["attempt"] == 1


def test_an_attempt_is_settled_by_its_outcome_or_by_a_run_no_retry_reaches():
    token = run_tokens.mint()
    open_run = _track(("op_submitted", {"op": {"run_id": "r", "steps": []}}), ("run_picked_up", {}),
                      ("step_started", {"step": "s", "attempt": 0}), _started(token))
    assert not intents.attempt_settled(open_run.replay("r"), "r:s:0")
    open_run.append(TrackEvent(run_id="r", event_type="interrupted", payload={"backend": "none"}, schema=1))
    assert not intents.attempt_settled(open_run.replay("r"), "r:s:0")  # a retry resumes this key
    assert intents.attempt_settled(open_run.replay("r"), "no key")  # text that names no attempt has none
    open_run.append(TrackEvent(run_id="r", event_type="step_failed", payload={"step": "s", "attempt": 0,
                                                                              "error": "outcome-unknown"}, schema=1))
    assert intents.attempt_settled(open_run.replay("r"), "r:s:0")
    cancelled = _track(("op_submitted", {"op": {"run_id": "r", "steps": []}}), ("run_picked_up", {}),
                       ("step_started", {"step": "s", "attempt": 0}), _started(token), ("cancelled", {"actor": "a"}))
    assert intents.attempt_settled(cancelled.replay("r"), "r:s:0")
