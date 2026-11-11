"""A stand-in for the built registration bundle, for tests that serve it.

The real bundle is built by ``admin-ui`` (``npm run build``) into
``<dist>/registration``. The tests that use this cover placement, gating and
headers, so they need files of the right shape in the right place and nothing
more; what the app renders is the front-end suite's business.
"""

from __future__ import annotations

import json
from pathlib import Path

# The font files the server-rendered pages' stylesheet names. The admin-ui
# build checks the real bundle contains every one (scripts/check-bundles.mjs),
# so a stand-in written from this list cannot drift from what the build makes.
WEB_FONT_FILES: list[str] = json.loads((Path(__file__).parents[1] / "admin-ui" / "web-fonts.json").read_text())["files"]

SHELL = (
    "<!doctype html><title>Accept your invitation</title>"
    # Exactly the shape Vite emits: document-relative, so how the document is
    # addressed decides where the browser looks for these.
    '<script type="module" crossorigin src="./assets/index-abc123.js"></script>'
    '<link rel="stylesheet" crossorigin href="./assets/index-abc123.css">'
    "<div id=root></div>"
)


def built_dist(tmp_path: Path) -> Path:
    """An ``admin_ui_dist`` directory holding a built registration bundle."""

    dist = tmp_path / "admin-ui-dist"
    bundle = dist / "registration"
    (bundle / "assets").mkdir(parents=True, exist_ok=True)
    (bundle / "index.html").write_text(SHELL)
    (bundle / "assets" / "index-abc123.js").write_text("console.log('registration')")
    (bundle / "assets" / "index-abc123.css").write_text("body{margin:0}")
    # The typeface files the build keeps under stable names, which the
    # server-rendered pages' stylesheet refers to as well.
    for name in WEB_FONT_FILES:
        (bundle / "assets" / name).write_bytes(b"wOF2 stand-in")
    return dist
