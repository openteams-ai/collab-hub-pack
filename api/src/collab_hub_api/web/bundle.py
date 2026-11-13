"""A built front-end bundle on disk: one document and the files beside it.

The browser surface serves two of these, both built by ``admin-ui``: the admin
panel and the registration pages. Each is a directory holding ``index.html``
and an ``assets`` directory of content-hashed files, and each is served through
explicit routes rather than a ``StaticFiles`` mount, because the surface refuses
a ``Mount`` under its prefixes at startup (a mounted sub-app's routing is opaque
to the checks that verify the surface is covered).

Serving through routes means the asset path is validated here rather than by
somebody else's implementation, and this module is the one place that happens:
the two routers that serve a bundle differ in who may reach it, never in how a
file name is turned into a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse

__all__ = ["ASSET_NAME", "BuiltBundle"]

ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
"""What an asset file may be called.

Vite emits hashed names from this alphabet. No slashes and no dots-only names,
so ``..`` and every path with a separator in it fail the match before anything
touches the filesystem.
"""


@dataclass(frozen=True)
class BuiltBundle:
    """One built bundle, known to have a document."""

    root: Path

    @classmethod
    def at(cls, root: Path) -> BuiltBundle | None:
        """The bundle at *root*, or ``None`` when no build is there.

        A directory whose ``index.html`` is missing counts as no build. That is
        what a skipped or failed build step produces, and the callers decide
        what a deployment without the bundle answers.
        """

        return cls(root) if (root / "index.html").is_file() else None

    def document(self) -> HTMLResponse:
        """The document the app boots from."""

        return HTMLResponse((self.root / "index.html").read_text(encoding="utf-8"))

    def asset(self, filename: str) -> FileResponse:
        """One file from the bundle's ``assets`` directory, or a 404."""

        assets = self.root / "assets"
        if not ASSET_NAME.match(filename) or filename in (".", ".."):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        target = (assets / filename).resolve()
        # The pattern already excludes separators, so this cannot currently
        # fail. It is kept because the pattern is the thing most likely to be
        # loosened by someone adding a file type, and a containment check that
        # only exists while the pattern is strict is a check that disappears
        # exactly when it starts to matter.
        if not target.is_file() or assets.resolve() not in target.parents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(target)
