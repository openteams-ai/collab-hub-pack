"""The SDK stands alone: it imports no hub package, and needs nothing beyond the standard library."""

import json
import subprocess
import sys


def test_the_sdk_imports_no_hub_package_and_no_third_party_module():
    program = (
        "import sys, json; before = set(sys.modules);"
        "import collab_hub_cog_worker, collab_hub_cog_worker.adapters;"
        "loaded = sorted(name for name in set(sys.modules) - before if not name.startswith('collab_hub_cog_worker'));"
        "print(json.dumps(loaded))"
    )
    output = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True).stdout
    loaded = json.loads(output)
    assert not [name for name in loaded if name.startswith(("collab_hub_api", "collab_hub_execution"))]
    third_party = [name for name in loaded if name.split(".")[0] not in sys.stdlib_module_names]
    assert not third_party, third_party
