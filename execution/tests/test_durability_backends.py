"""Durability backends: chosen by configuration, never imported, and all fed the same step functions."""

from __future__ import annotations

import ast
import re
from datetime import timedelta
from pathlib import Path

import pytest

from collab_hub_execution import (
    DURABILITY_BACKENDS,
    STEP_FUNCTIONS,
    BackendNotImplemented,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunBudget,
)
from collab_hub_execution.backends import select_backend

SOURCE = Path(__file__).resolve().parents[1] / "src" / "collab_hub_execution"


def _built() -> list[str]:
    built = []
    for name in DURABILITY_BACKENDS:
        try:
            select_backend(name)
        except BackendNotImplemented:
            continue
        built.append(name)
    return built


BUILT = _built()


def test_the_backend_setting_takes_three_values_and_none_is_built():
    assert DURABILITY_BACKENDS == ("none", "dbos", "temporal")
    assert BUILT == ["none"]
    runner = LifecycleRunner(executor=InMemoryCogExecutor({}), track=InMemoryTrackStore())
    assert runner.backend.name == "none" and runner.backend.durable is False


@pytest.mark.parametrize(("name", "phase"), [("dbos", "Phase 27"), ("temporal", "Phase 33")])
def test_a_backend_not_built_yet_is_refused_when_the_runner_starts(name, phase):
    with pytest.raises(BackendNotImplemented, match=phase):
        LifecycleRunner(executor=InMemoryCogExecutor({}), track=InMemoryTrackStore(), backend=name)


def test_an_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="unknown durability backend 'redis'"):
        LifecycleRunner(executor=InMemoryCogExecutor({}), track=InMemoryTrackStore(), backend="redis")


def _backend_imports(path: Path) -> list[str]:
    """Every import of a concrete backend in one file: a module below ``backends``, or a backend class."""
    source = path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # The API targets a newer Python than this package's floor, whose parser may refuse its
        # syntax (PEP 701 f-strings on 3.11); the same rule, read line by line.
        pattern = re.compile(r"^\s*(?:from\s+\S*\bbackends\.\w+|import\s+\S*\bbackends\.\w+|from\s+\S*\bbackends\s+"
                             r"import\s+.*\b(?!DurabilityBackend\b)\w+Backend\b)")
        return [line.strip() for line in source.splitlines() if pattern.search(line)]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and ".backends." in f".{node.module}.":
            if node.module.split(".")[-1] != "backends":
                found.append(f"from {node.module}")
            if any(alias.name.endswith("Backend") and alias.name != "DurabilityBackend" for alias in node.names):
                found.append(f"imports {[a.name for a in node.names]}")
    return found


def test_no_caller_imports_a_concrete_backend():
    # The configuration value is the only switch: nothing outside backends/ reaches a backend module.
    repository = SOURCE.parents[2]
    roots = [SOURCE, repository / "scripts", repository / "api" / "src"]
    assert all(root.is_dir() for root in roots), roots  # a wrong path would scan nothing and pass
    offenders, scanned = [], set()
    for root in roots:
        for path in root.rglob("*.py"):
            scanned.add(root)
            if SOURCE / "backends" in path.parents:
                continue
            offenders += [f"{path}: {found}" for found in _backend_imports(path)]
    assert scanned == set(roots) and offenders == []


def test_the_boundary_check_finds_a_concrete_backend_import_either_way(tmp_path):
    # Both readings of the rule catch the imports it forbids, and pass the ones it allows.
    for body, caught in (("from collab_hub_execution.backends.none import NoneBackend\n", True),
                         ("from collab_hub_execution.backends import NoneBackend\n", True),
                         ("from collab_hub_execution.backends import DurabilityBackend, select_backend\n", False)):
        path = tmp_path / "caller.py"
        path.write_text(body)
        assert bool(_backend_imports(path)) is caught, body
        path.write_text(body + 'x = f"{\'\\\\\' + \'a\'!r}"\n')  # unparseable before 3.12: the line reading
        if _parses(path):
            continue
        assert bool(_backend_imports(path)) is caught, body


def _parses(path: Path) -> bool:
    try:
        ast.parse(path.read_text())
    except SyntaxError:
        return False
    return True


def test_a_backend_reimplements_no_part_of_the_driver():
    # A backend only runs what it is handed: no import of the machines or the runner, and no
    # function beyond run_step, whose whole body is to call the step function.
    for path in (SOURCE / "backends").glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not imported & {"states", "runner", "ops", ".states", ".runner", ".ops"}, (path, imported)
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert [f.name for f in functions] == ["run_step"], path
        [body] = [n for n in functions[0].body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        assert ast.unparse(body) == "return function(*args, **kwargs)", path


def _scenarios(runner_for):
    """Runs that reach every step function: completion, escalation and approval, failures, budget stops."""
    runner = runner_for({"c": lambda entry, value: value})
    runner.submit(OpDefinition("gated", (OpStep("s", "c", "run", 1, gate=Gate(escalate="always")),)))
    runner.decide("gated", escalation=runner.open_escalation("gated")["escalation"], actor="alice", outcome="approve")
    runner_for({"c": lambda entry, value: ResultEnvelope.failure("model-call-failed", "no")}).submit(
        OpDefinition("refused", (OpStep("s", "c", "run"),)))
    runner_for({"c": lambda entry, value: ResultEnvelope.success(value, usage={"tokens": 10})},
               budget=RunBudget(max_tokens=5)).submit(
        OpDefinition("spent", (OpStep("a", "c", "run"), OpStep("b", "c", "run"))))
    runner_for({"c": lambda entry, value: value}, budget=RunBudget(max_duration=timedelta(0))).submit(
        OpDefinition("late", (OpStep("s", "c", "run"),)))


@pytest.mark.parametrize("backend", BUILT)
def test_every_backend_runs_every_step_function_and_only_through_run_step(backend):
    driven: list[str] = []  # what the driver handed the backend
    ran: list[str] = []  # what actually ran

    def runner_for(handlers, **kwargs):
        runner = LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=InMemoryTrackStore(),
                                 backend=backend, **kwargs)
        real_run_step = runner.backend.run_step

        def run_step(run_id, name, function, *args, **kw):
            driven.append(name)
            return real_run_step(run_id, name, function, *args, **kw)

        runner.backend.run_step = run_step
        for name in STEP_FUNCTIONS:
            bound = getattr(runner, name)

            def spy(*args, _bound=bound, _name=name, **kw):
                ran.append(_name)
                return _bound(*args, **kw)

            setattr(runner, name, spy)
        return runner

    _scenarios(runner_for)
    assert set(ran) == set(STEP_FUNCTIONS)
    assert driven == ran  # every step function went through the backend, and nothing else did
