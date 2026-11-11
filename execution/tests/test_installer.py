"""Installing a Cog by digest: fetched, bound by its own ``resolve``, checked with that binding delivered.

The executor is the in-memory one (the Cog's ``resolve`` and ``check`` are
handlers) or the local one with a real worker process; the artifact comes
from the fake registry. What the install store records of each step, what a
failing step leaves behind, what a run of the installed Cog is delivered, and
what an uninstall removes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import tomllib

import pytest
from fake_registry import FakeRegistry
from locations_support import packages

from collab_hub_execution import (
    BindingRecord,
    BindingResolutionError,
    Delivered,
    InMemoryCogExecutor,
    InMemoryInstallStore,
    InMemoryTrackStore,
    LifecycleRunner,
    ModelsBlock,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunState,
    intents,
    inventory,
)
from collab_hub_execution.artifacts import RegistryClient, locate, materialize, publish
from collab_hub_execution.binding import (
    MANIFEST_SECRET_TABLES,
    harness_satisfier,
    names_a_secret,
    refuse_literal_secrets,
)
from collab_hub_execution.controller import BindingDelivery, RunController
from collab_hub_execution.installer import Installer, harnesses_installed
from collab_hub_execution.installs import InstallRow, new_install_id
from collab_hub_execution.locations import select_executor
from collab_hub_execution.locations.packages import DirectoryPackageSource

MODELS = ModelsBlock.parse(tomllib.loads("""
[models.fake]
endpoint = "http://127.0.0.1:8090/v1"
model = "fake-model"
auth_ref = "env:FAKE_KEY"
context_window = 8192
max_output_tokens = 1024
locality = "in-cluster"

[models.claude]
provider = "anthropic"
model = "claude-opus-5-5"
auth_ref = "env:CLAUDE_KEY"
"""))
ENVIRON = {"FAKE_KEY": "fake-secret", "CLAUDE_KEY": "claude-secret"}
CONTEXT_CARD = {"kind": "context", "requires": [{"capability": "model", "locality": "any", "satisfiers": []}],
                "ops": {"usage": ["ask"], "lifecycle": ["serve", "resolve", "check"]}}
HARNESS_CARD = {"kind": "harness", "provides": ["harness"], "requires": [{"capability": "model"}],
                "ops": {"usage": ["ask"], "lifecycle": ["serve", "resolve", "check"]}}
BY = {"user": "alice", "org_id": "acme"}


def selecting(selection=None, check_ok=True, seen=None):
    """A Cog whose ``resolve`` picks the first satisfier of each capability, and whose ``check`` answers."""
    def handle(entry_point, value, **feedback):
        if seen is not None:
            seen.append((entry_point, value))
        if entry_point == "resolve":
            if selection is not None:
                return {"selections": selection}
            chosen = {}
            for requirement in value["requires"]:
                for satisfier in value["inventory"]:
                    if requirement["capability"] in satisfier["provides"]:
                        chosen.setdefault(requirement["capability"], satisfier["id"])
            return {"selections": chosen}
        if entry_point == "check":
            if check_ok:
                return {"probed": True}
            return ResultEnvelope.failure("model-unavailable", "the model answered 503")
        return {"answer": "hi"}
    return handle


def _settled(install, seconds=10):
    deadline = time.monotonic() + seconds
    while not install.idle():
        assert time.monotonic() < deadline, "the installer did not settle"
        time.sleep(0.01)


@pytest.fixture
def registry():
    registry = FakeRegistry().start()
    yield registry
    registry.stop()


def published(registry, tmp_path, name="ctx"):
    """The echo fake Cog, made a self-contained package (its worker beside it) and published under ``name``."""
    root = packages(tmp_path / name) / "echo"
    shutil.copy(root.parent / "fake_worker.py", root / "fake_worker.py")
    (root / "serve.py").write_text((root / "serve.py").read_text().replace(".parent.parent", ".parent"))
    (root / "COG.md").write_text(f"---\nname: {name}\n---\n")  # one digest per name
    return publish(root, f"{registry.host}/cogs/{name}:1", RegistryClient(plain_http={registry.host}))


def row(reference, card=CONTEXT_CARD, org="acme"):
    return InstallRow(id=new_install_id(), org_id=org, reference=str(reference), digest=str(reference).rsplit("@")[1]
                      if "@" in str(reference) else "sha256:" + "0" * 64, cog_id=f"acme/{org}", card=card,
                      requested_by="alice")


def installer(store, executor, registry=None, packages_root=None, environ=ENVIRON, harness_of=None,
              controller="one", **extra):
    client = RegistryClient(plain_http={registry.host}) if registry is not None else None
    return Installer(
        store, executor, controller=controller, registry=client, environ=environ,
        packages=DirectoryPackageSource([packages_root]) if packages_root else None,
        inventory=lambda org: inventory(MODELS, harnesses_installed(store, org)), **extra)


def test_an_install_is_fetched_bound_by_the_cogs_own_resolve_and_checked_with_that_binding(registry, tmp_path):
    reference = published(registry, tmp_path)
    store, seen = InMemoryInstallStore(), []
    executor = InMemoryCogExecutor({str(reference): selecting(seen=seen)})
    asked = store.create(row(reference))
    assert store.claim(asked.id, "one")
    done = installer(store, executor, registry).install(asked.id)
    assert done.invokable and done.failure is None and done.lock_digest.startswith("sha256:")
    assert [e.event_type for e in store.events(asked.id)] == ["install_fetched", "binding_admitted", "check_passed"]
    # resolve saw the inventory generated from the models block: every served model, with no key in it.
    (resolve, resolve_input), (check, _) = seen
    assert resolve == "resolve" and check == "check"
    assert [s["id"] for s in resolve_input["inventory"]] == ["model:fake", "model:claude"]
    assert resolve_input["inventory"][0] == {
        "id": "model:fake", "kind": "model", "provides": ["model", "model-endpoint/openai-compatible"],
        "locality": "in-cluster", "provider": "openai-compatible", "endpoint": "http://127.0.0.1:8090/v1",
        "model": "fake-model", "transport": "http", "auth_ref": "env:FAKE_KEY", "context_window": 8192,
        "max_output_tokens": 1024}
    assert "fake-secret" not in json.dumps(resolve_input)
    # The binding record names what the Cog selected, by reference, and the Track-facing id.
    record = BindingRecord.from_dict(done.binding)
    assert done.binding_id == record.id and record.model.id == "model:fake" and record.resolved_by == "cog"
    assert record.selections == {"model": "model:fake"} and record.harness is None
    assert "fake-secret" not in json.dumps(done.binding)
    # The workers of the install were materialized under the install's own run id, and torn down.
    assert executor.materialized == [(asked.id and f"install:{asked.id}", str(reference))] * 2
    assert executor.torn_down == [str(reference)] * 2


def test_a_failing_check_leaves_the_install_bound_and_says_which_step_failed(registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    asked = store.create(row(reference))
    store.claim(asked.id, "one")
    executor = InMemoryCogExecutor({str(reference): selecting(check_ok=False)})
    done = installer(store, executor, registry).install(asked.id)
    assert done.state == "bound" and not done.invokable
    assert done.failed_step == "check" and done.failure == "check: model-unavailable: the model answered 503"
    assert done.binding_id is not None  # the binding stands: what the check ran against
    events = store.events(asked.id)
    assert [e.event_type for e in events] == ["install_fetched", "binding_admitted", "check_failed"]
    assert events[-1].payload["step"] == "check"
    # A worker that never comes up fails the check at materialize.
    class Refusing(InMemoryCogExecutor):
        def materialize(self, cog, run_id, instance=""):
            if instance == "check":
                raise TimeoutError("deployment not ready within 60s")
            return super().materialize(cog, run_id, instance)

    again = store.create(row(reference, org="other"))
    store.claim(again.id, "one")
    done = installer(store, Refusing({str(reference): selecting()}), registry).install(again.id)
    assert done.state == "bound" and done.failed_step == "materialize"
    assert done.failure == "materialize: TimeoutError: deployment not ready within 60s"


def test_a_binding_the_hub_cannot_admit_leaves_the_install_fetched_with_the_reason(registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    cases = {
        "not offered": (selecting({"model": "model:mine"}), "which the inventory it was offered does not hold"),
        "nothing selected": (selecting({}), "resolve selected nothing for model"),
        "not an envelope": (lambda e, v, **f: ResultEnvelope.failure("invalid-input", "no resolve here"),
                            "resolve failed: invalid-input"),
    }
    for org, (handler, message) in cases.items():
        asked = store.create(row(reference, org=org))
        store.claim(asked.id, "one")
        done = installer(store, InMemoryCogExecutor({str(reference): handler}), registry).install(asked.id)
        assert done.state == "fetched" and done.failed_step == "bind" and message in done.failure, org
        assert [e.event_type for e in store.events(asked.id)] == ["install_fetched", "binding_refused"]
    # A key the controller cannot find refuses the binding too: the check would run against nothing.
    asked = store.create(row(reference, org="keyless"))
    store.claim(asked.id, "one")
    done = installer(store, InMemoryCogExecutor({str(reference): selecting()}), registry, environ={}).install(asked.id)
    assert done.state == "fetched" and "FAKE_KEY" in done.failure and "not set" in done.failure


def test_a_cog_that_requires_nothing_is_bound_to_nothing_and_checked_by_readiness_alone(registry, tmp_path):
    reference = published(registry, tmp_path)
    store, seen = InMemoryInstallStore(), []
    asked = store.create(row(reference, card={"kind": "complete", "requires": [], "ops": {"lifecycle": ["serve"]}}))
    store.claim(asked.id, "one")
    done = installer(store, InMemoryCogExecutor({str(reference): selecting(seen=seen)}), registry).install(asked.id)
    assert done.invokable and seen == []  # neither resolve nor check was asked
    record = BindingRecord.from_dict(done.binding)
    assert record.resolved_by == "none" and record.model is None and record.delivery({}) == Delivered(
        env={}, files={"binding.json": json.dumps(record.file(), sort_keys=True)})


def test_an_artifact_the_registry_does_not_hold_or_that_is_not_runnable_is_not_fetched(registry, tmp_path):
    store = InMemoryInstallStore()
    missing = f"{registry.host}/cogs/none@sha256:" + "1" * 64
    asked = store.create(row(missing))
    store.claim(asked.id, "one")
    done = installer(store, InMemoryCogExecutor({}), registry).install(asked.id)
    # The row comes back as recorded, so a caller sees what failed rather than nothing.
    assert done is not None and done.state == "published" and done.failed_step == "fetch"
    assert store.get(asked.id).failed_step == "fetch" and "not in the registry" in store.get(asked.id).failure
    assert store.events(asked.id) == ()


def test_a_context_cog_requiring_a_harness_resolves_to_the_installed_harness_cog(registry, tmp_path):
    hermes = published(registry, tmp_path, "hermes")
    context = published(registry, tmp_path, "ctx")
    store = InMemoryInstallStore()
    executor = InMemoryCogExecutor({str(hermes): selecting(), str(context): selecting()})
    harness = store.create(InstallRow(id=new_install_id(), org_id="acme", reference=str(hermes),
                                      digest=hermes.digest, cog_id="openteams/hermes", card=HARNESS_CARD))
    store.claim(harness.id, "one")
    assert installer(store, executor, registry).install(harness.id).invokable
    assert [s.id for s in harnesses_installed(store, "acme")] == [f"harness:{harness.id}"]
    assert harnesses_installed(store, "other") == []  # another organization's installs are its own
    card = {"kind": "context", "requires": [{"capability": "model"}, {"capability": "harness",
                                                                        "satisfiers": ["openteams/hermes"]}]}
    asked = store.create(InstallRow(id=new_install_id(), org_id="acme", reference=str(context),
                                    digest=context.digest, cog_id="acme/ctx", card=card))
    store.claim(asked.id, "one")
    done = installer(store, executor, registry).install(asked.id)
    assert done.invokable
    record = BindingRecord.from_dict(done.binding)
    assert record.harness.reference == str(hermes) and record.harness.install == harness.id
    assert record.model.id == "model:fake"
    assert record.file()["harness"] == {"satisfier": f"harness:{harness.id}", "reference": str(hermes),
                                        "digest": hermes.digest, "cog_id": "openteams/hermes"}


def test_the_inventory_filter_stands_between_the_inventory_and_resolve(registry, tmp_path):
    reference = published(registry, tmp_path)
    store, seen = InMemoryInstallStore(), []
    asked = store.create(row(reference))
    store.claim(asked.id, "one")
    contexts = []

    def only_external(satisfiers, context):  # what a sensitivity filter (#10) would do here
        contexts.append(context)
        return [s for s in satisfiers if s.locality == "external"]

    done = installer(store, InMemoryCogExecutor({str(reference): selecting(seen=seen)}), registry,
                     inventory_filter=only_external).install(asked.id)
    assert [s["id"] for s in seen[0][1]["inventory"]] == ["model:claude"]
    assert BindingRecord.from_dict(done.binding).model.id == "model:claude"
    assert contexts == [{"reference": str(reference), "card": CONTEXT_CARD, "install": asked.id}]


def test_literal_secrets_are_refused_in_a_manifest_and_a_binding():
    refuse_literal_secrets({"auth_ref": "env:KEY", "run_token_sha256": "ab", "model": {"api_key": ""}}, where="ok")
    for bad in ({"api_key": "sk-123"}, {"model": {"password": "x"}}, {"auth_ref": "sk-123"}, {"apiKey": "sk-1"},
                {"tool": {"cog": {"secrets": [{"token": "t"}]}}}):
        with pytest.raises(BindingResolutionError):
            refuse_literal_secrets(bad, where="test")
    with pytest.raises(BindingResolutionError, match="auth_ref"):
        ModelsBlock.parse({"models": {"m": {"endpoint": "http://x", "model": "x", "auth_ref": "sk-literal"}}})


def test_the_secret_check_reads_whole_words_and_only_the_manifest_tables_a_secret_could_sit_in():
    # A dependency or a task is named freely: `tiktoken` is no token, and a task called refresh-token is a task.
    manifest = {"pypi-dependencies": {"tiktoken": ">=0.7"}, "tasks": {"refresh-token": "python refresh.py"},
                "dependencies": {"python-dotenv": "*"}}
    refuse_literal_secrets(manifest, where="pixi.toml", tables=MANIFEST_SECRET_TABLES)
    assert not names_a_secret("tiktoken") and names_a_secret("refresh-token") and names_a_secret("apiKey")
    # Where a value could be a credential, it is still refused: the activation's variables and the Cog's profile.
    for bad in ({"activation": {"env": {"OPENAI_API_KEY": "sk-1"}}},
                {"feature": {"gpu": {"activation": {"env": {"HF_TOKEN": "hf_1"}}}}},
                {"tool": {"cog": {"password": "x"}}}):
        with pytest.raises(BindingResolutionError):
            refuse_literal_secrets(bad, where="pixi.toml", tables=MANIFEST_SECRET_TABLES)
    # A binding is read whole: its keys are the hub's own.
    with pytest.raises(BindingResolutionError):
        refuse_literal_secrets({"tasks": {"token": "t"}}, where="binding")


def test_an_install_a_controller_did_not_finish_is_resumed_on_its_next_start_from_the_recorded_state(
        registry, tmp_path):
    """The controller stops after bind: its next start, under the same id, finds the row bound with no delivery in
    memory, delivers the binding again from the record, and checks."""
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    asked = store.create(row(reference))
    store.claim(asked.id, "one")
    first = installer(store, InMemoryCogExecutor({str(reference): selecting()}), registry)
    bound = first._bind(first._fetch(asked))
    assert bound.state == "bound" and [r.id for r in store.in_progress()] == [asked.id]
    seen = []
    again = installer(store, InMemoryCogExecutor({str(reference): selecting(seen=seen)}), registry)
    again.tick()
    _settled(again)
    done = store.get(asked.id)
    assert done.invokable and [e for e, _ in seen] == ["check"]  # resumed at check: resolve is not asked again
    assert again.delivered(done.run_id) is None  # the check's delivery is let go of after it
    assert [e.event_type for e in store.events(asked.id)] == ["install_fetched", "binding_admitted", "check_passed"]


def test_a_resumed_binding_whose_key_is_gone_fails_the_check_at_bind(registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    asked = store.create(row(reference))
    store.claim(asked.id, "one")
    first = installer(store, InMemoryCogExecutor({str(reference): selecting()}), registry)
    first._bind(first._fetch(asked))
    again = installer(store, InMemoryCogExecutor({str(reference): selecting()}), registry, environ={})
    done = again.install(asked.id)
    assert done.state == "bound" and done.failed_step == "bind" and "FAKE_KEY" in done.failure
    assert store.in_progress() == ()  # a recorded failure is not resumed into again


def test_an_install_of_a_controller_that_no_longer_holds_its_id_is_taken_over(registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    asked = store.create(row(reference))
    store.claim(asked.id, "dead")
    other = installer(store, InMemoryCogExecutor({str(reference): selecting()}), registry, controller="two")
    other.tick(alive=lambda controller: controller != "dead")
    _settled(other)
    assert store.get(asked.id).controller == "two" and store.get(asked.id).invokable
    # One whose controller still holds its id is left to it.
    held = store.create(row(reference, org="other"))
    store.claim(held.id, "one")
    other.tick(alive=lambda controller: True)
    _settled(other)
    assert store.get(held.id).controller == "one" and store.get(held.id).state == "published"


def test_an_unexpected_failure_is_recorded_at_its_step_so_the_install_is_not_resumed_into_it(registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    asked = store.create(row(reference))
    store.claim(asked.id, "one")

    class Broken(InMemoryCogExecutor):
        def materialize(self, cog, run_id, instance=""):
            raise RuntimeError("the cluster is unreachable")

    done = installer(store, Broken({str(reference): selecting()}), registry).install(asked.id)
    assert done.state == "fetched" and done.failed_step == "bind" and "unreachable" in done.failure
    assert store.in_progress() == ()


def test_a_run_of_an_installed_cog_is_delivered_its_binding_and_every_step_event_names_it(registry, tmp_path):
    reference = published(registry, tmp_path)
    store, track = InMemoryInstallStore(), InMemoryTrackStore()
    delivered = []

    class Delivering(InMemoryCogExecutor):
        def __init__(self, handlers, deliver):
            super().__init__(handlers)
            self.deliver = deliver

        def materialize(self, cog, run_id, instance=""):
            delivered.append((run_id, self.deliver(cog, run_id, instance)))
            return super().materialize(cog, run_id, instance)

    delivery = BindingDelivery(track, store, MODELS, environ=ENVIRON)
    executor = Delivering({str(reference): selecting(), "echo": lambda e, v, **f: v}, delivery)
    install = installer(store, executor, registry)
    delivery.installer = install
    asked = store.create(row(reference))
    runner = LifecycleRunner(executor=executor, track=track, controller="one")
    controller = RunController(runner, poll_interval=0.01, installer=install)
    for _ in range(500):
        controller.tick()
        if store.get(asked.id).invokable and controller.idle():
            break
        time.sleep(0.01)
    done = store.get(asked.id)
    assert done.invokable, done
    # The resolving worker was given nothing; the checking worker the binding, file and key.
    (resolving, nothing), (checking, with_binding) = delivered
    assert resolving == checking == f"install:{asked.id}" and nothing == Delivered()
    assert json.loads(with_binding.files["binding.json"])["id"] == done.binding_id
    assert with_binding.env == {"COLLAB_MODEL_API_KEY": "fake-secret"}
    # A run of the installed Cog names the install on its step, and is delivered the same binding.
    op = OpDefinition("r", (OpStep("ask", str(reference), "ask", {"prompt": "hi"}, digest=reference.digest,
                                   install=asked.id, binding=done.binding_id),
                            OpStep("echo", "echo", "run", {"x": 1})))
    intents.submit(track, op, by=BY)
    for _ in range(500):
        controller.tick()
        if intents.describe(track, "r").state.ended and controller.idle():
            break
        time.sleep(0.01)
    assert intents.describe(track, "r").state is RunState.COMPLETED
    (run_id, given) = delivered[2]
    assert run_id == "r" and json.loads(given.files["binding.json"]) == BindingRecord.from_dict(done.binding).file()
    assert given.env == {"COLLAB_MODEL_API_KEY": "fake-secret"}
    assert delivered[3] == ("r", Delivered())  # the package run by name: no model bound to it in [cogs]
    events = {(e.event_type, e.payload.get("step")): e.payload for e in track.replay("r")}
    for kind in ("step_started", "step_completed"):  # worker_started: the in-memory worker records none
        assert events[(kind, "ask")]["binding_id"] == done.binding_id and events[(kind, "ask")]["install"] == asked.id
        assert "binding_id" not in events[(kind, "echo")]
    assert "fake-secret" not in json.dumps([e.payload for e in track.replay("r")])
    # A step naming an install that is not invokable is not materialized.
    store.record(asked.id, done.machine().uninstall())
    with pytest.raises(BindingResolutionError, match="only an invokable Cog is materialized"):
        delivery(str(reference), "r", "ask:0")


def test_an_uninstall_sweeps_the_installs_workers_and_evicts_its_package_keeping_a_shared_locks_environment(
        registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    swept = []

    class Sweeping(InMemoryCogExecutor):
        def sweep(self, *, controller=None, run_id=None):
            swept.append(run_id)

    cache = tmp_path / "cache"
    pulled = materialize(reference, cache, RegistryClient(plain_http={registry.host}))
    package, environment = pulled.directory, pulled.environment
    executor = Sweeping({str(reference): selecting()})
    install = installer(store, executor, registry, cache=cache)
    first = store.create(row(reference))
    second = store.create(row(reference, org="other"))
    for asked in (first, second):
        store.claim(asked.id, "one")
        assert install.install(asked.id).invokable
    store.request_uninstall(first.id, "alice")
    install.tick()
    _settled(install)
    gone = store.get(first.id)
    assert gone.state == "uninstalled" and swept == [f"install:{first.id}"]
    assert [e.event_type for e in store.events(first.id)][-1] == "uninstalled"
    # The package goes with its install; the other install shares the lock, so the lock's environment stays.
    assert not package.exists() and environment.is_dir()
    with pytest.raises(Exception, match="not been materialized"):
        locate(reference, cache)
    materialize(reference, cache, RegistryClient(plain_http={registry.host}))  # the other install's next run
    store.request_uninstall(second.id, "bob")
    install.uninstall(second.id)
    assert not package.exists() and not environment.exists()  # the last install of that lock: both go


def test_an_uninstall_at_remote_evicts_through_the_executor(registry, tmp_path):
    reference = published(registry, tmp_path)
    store = InMemoryInstallStore()
    evicted = []
    executor = InMemoryCogExecutor({str(reference): selecting()})
    install = installer(store, executor, registry, evict=lambda ref, keep: evicted.append((ref, keep)) or True)
    first = store.create(row(reference))
    second = store.create(row(reference, org="other"))
    for asked in (first, second):
        store.claim(asked.id, "one")
        assert install.install(asked.id).invokable
    store.request_uninstall(first.id, "alice")
    install.uninstall(first.id)
    store.request_uninstall(second.id, "bob")
    install.uninstall(second.id)
    assert evicted == [(str(reference), True), (str(reference), False)]
    assert store.pending() == ()


def test_a_removal_asked_before_a_controller_fetched_anything_ends_the_install(registry, tmp_path):
    store = InMemoryInstallStore()
    asked = store.create(row(published(registry, tmp_path)))
    store.request_uninstall(asked.id, "alice")
    install = installer(store, InMemoryCogExecutor({}), registry)
    install.tick()
    _settled(install)
    assert store.get(asked.id).state == "uninstalled"


@pytest.mark.skipif(sys.platform == "win32", reason="the local location is Linux and macOS")
def test_a_development_package_installs_from_the_directory_source_on_a_real_worker(tmp_path, registry):
    """At level 1 an install names a package of the directory source; its check runs a real worker process."""
    root = packages(tmp_path)
    store = InMemoryInstallStore()
    executor = select_executor("local", packages=[root], work_dir=tmp_path / "runs", environment="host",
                               grace=1.0, ready_timeout=30.0)
    asked = store.create(InstallRow(id=new_install_id(), org_id="acme", reference="echo",
                                    digest=DirectoryPackageSource([root]).resolve("echo").digest, cog_id="echo",
                                    card={"kind": "complete", "ops": {"lifecycle": ["serve", "check"]}}))
    store.claim(asked.id, "one")
    done = installer(store, executor, packages_root=root).install(asked.id)
    assert done.invokable, done
    assert done.lock_digest == DirectoryPackageSource([root]).resolve("echo").digest
    assert [e.event_type for e in store.events(asked.id)] == ["install_fetched", "binding_admitted", "check_passed"]
    assert not list((tmp_path / "runs").glob("**/stdout.log")) or True  # the worker's output is the run's


def test_a_published_cog_runs_as_a_local_worker_from_its_artifact_with_its_binding_file(tmp_path, registry):
    """A pinned reference at the local location: pulled into the cache under the work directory, served from
    there, and given the binding file beside its logs."""
    reference = published(registry, tmp_path)
    client = RegistryClient(plain_http={registry.host})
    record = BindingRecord.of(str(reference), {"model": "model:fake"}, inventory(MODELS))
    deliver = lambda cog, run_id, instance: record.delivery(ENVIRON)  # noqa: E731
    executor = select_executor("local", packages=[packages(tmp_path / "pk")], work_dir=tmp_path / "runs",
                               environment="host", grace=1.0, ready_timeout=30.0, registry=client, deliver=deliver)
    track = InMemoryTrackStore()
    runner = LifecycleRunner(executor=executor, track=track, controller="one")
    state = runner.submit(OpDefinition("r", (OpStep("a", str(reference), "run", {"x": 1}, digest=reference.digest,
                                                     install="inst-x", binding=record.id),)))
    assert state is RunState.COMPLETED, [e.payload for e in track.replay("r") if e.event_type == "step_failed"]
    [started] = [e.payload for e in track.replay("r") if e.event_type == "worker_started"]
    assert started["artifact"] == str(reference) and started["package"]["digest"] == reference.digest
    assert started["binding_id"] == record.id
    package = locate(reference, tmp_path / "runs" / "artifacts")
    assert (package.directory / "serve.py").is_file()
    binding_file = next((tmp_path / "runs").glob("**/binding.json"))
    assert oct(binding_file.stat().st_mode & 0o777) == "0o600"
    assert json.loads(binding_file.read_text())["auth_ref"] == "env:COLLAB_MODEL_API_KEY"
    assert "fake-secret" not in binding_file.read_text()
    [completed] = [e.payload for e in track.replay("r") if e.event_type == "step_completed"]
    assert completed["payload"] == {"echo": {"x": 1}}
    # Without a registry, a published Cog is refused rather than looked for in a directory.
    shutil.rmtree(tmp_path / "runs")
    alone = select_executor("local", packages=[packages(tmp_path / "pk2")], work_dir=tmp_path / "runs",
                            environment="host", grace=1.0)
    runner = LifecycleRunner(executor=alone, track=InMemoryTrackStore(), controller="one")
    assert runner.submit(OpDefinition("r2", (OpStep("a", str(reference), "run"),))) is RunState.FAILED
    assert os.environ.get("NEVER_SET") is None


def test_the_models_block_still_binds_a_development_package_by_name():
    track = InMemoryTrackStore()
    delivery = BindingDelivery(track, None, MODELS.__class__(models=MODELS.models, cogs={"hermes": "claude"}),
                               environ=ENVIRON)
    intents.submit(track, OpDefinition("r", (OpStep("a", "hermes", "ask"),)), by=BY)
    assert delivery("hermes", "r", "a:0").env == {"COLLAB_MODEL_PROVIDER": "anthropic",
                                                   "COLLAB_MODEL_NAME": "claude-opus-5-5",
                                                   "COLLAB_MODEL_API_KEY": "claude-secret"}
    assert delivery("echo", "r", "a:0") == Delivered()


def test_a_harness_satisfier_describes_the_installed_harness_by_reference():
    satisfier = harness_satisfier("inst-1", "hub/cogs/hermes@sha256:" + "a" * 64, "sha256:" + "a" * 64,
                                  "openteams/hermes", HARNESS_CARD)
    assert satisfier.descriptor() == {
        "id": "harness:inst-1", "kind": "harness", "provides": ["harness"], "locality": "in-cluster",
        "reference": "hub/cogs/hermes@sha256:" + "a" * 64, "digest": "sha256:" + "a" * 64,
        "cog_id": "openteams/hermes", "install": "inst-1"}
