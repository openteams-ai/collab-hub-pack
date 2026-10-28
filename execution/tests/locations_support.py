"""What the location tests share: the fake Cog packages of ``dev/cogs``, runnable without pixi."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import time
from pathlib import Path

from collab_hub_execution import InMemoryCogExecutor, ResultEnvelope
from collab_hub_execution.locations import select_executor

REPOSITORY = Path(__file__).resolve().parents[2]
DEV_COGS = REPOSITORY / "dev" / "cogs"

ENV_COG = '''"""env: answers with its own environment, to see what a worker was given."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fake_worker import envelope, serve


def handle(entry_point, value, **feedback):
    return envelope({"env": dict(os.environ)})


if __name__ == "__main__":
    serve(handle)
'''


def packages(tmp_path: Path) -> Path:
    """A copy of the fake Cog packages whose ``serve`` task runs under this interpreter, so no pixi is needed."""
    root = tmp_path / "cogs"
    shutil.copytree(DEV_COGS, root, ignore=shutil.ignore_patterns(".pixi", "__pycache__"))
    (root / "env").mkdir()
    (root / "env" / "serve.py").write_text(ENV_COG)
    for file in ("pixi.toml", "pixi.lock"):
        shutil.copy(root / "echo" / file, root / "env" / file)
    for manifest in root.glob("*/pixi.toml"):
        manifest.write_text(re.sub(r'^serve = .*$', f'serve = "{sys.executable} serve.py"', manifest.read_text(),
                                   flags=re.M))
    return root


def local_executor(tmp_path: Path, **settings):
    settings.setdefault("environment", "host")
    settings.setdefault("grace", 1.0)
    settings.setdefault("ready_timeout", 30.0)  # a worker's start is slower on a shared macOS runner
    return select_executor("local", packages=[packages(tmp_path)], work_dir=tmp_path / "runs", **settings)


def in_memory_executor() -> InMemoryCogExecutor:
    handlers = {}
    for serve in DEV_COGS.glob("*/serve.py"):
        spec = importlib.util.spec_from_file_location(f"fake_cog_{serve.parent.name.replace('-', '_')}", serve)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handlers[serve.parent.name] = (
            lambda entry, value, _handle=module.handle, **feedback: ResultEnvelope.parse(
                _handle(entry, value, **feedback)))
    return InMemoryCogExecutor(handlers)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed process whose parent has not reaped it yet still answers; it runs nothing.
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        pass
    # No /proc (macOS): ps says whether the process is a zombie.
    import subprocess

    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(state) and not state.startswith("Z")


def gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False
