"""The hub depends on the seam, never on the worker SDK (ADR-0002 invariant 4): nothing here imports it."""

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
HUB_SOURCES = (REPOSITORY / "execution" / "src", REPOSITORY / "api" / "src")


def test_no_hub_source_names_the_worker_sdk():
    offenders = [path for root in HUB_SOURCES for path in root.rglob("*.py")
                 if "collab_hub_cog_worker" in path.read_text(errors="replace")]
    assert not offenders, offenders


def test_importing_the_execution_package_loads_no_worker_sdk_module():
    program = ("import sys, json; import collab_hub_execution, collab_hub_execution.controller;"
               "print(json.dumps(sorted(m for m in sys.modules if m.startswith('collab_hub_cog_worker'))))")
    output = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True).stdout
    assert output.strip() == "[]"
