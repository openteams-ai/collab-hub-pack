"""collab-hub: a command-line client for the Collab Hub.

A client, not a second implementation: every command is an HTTP call to the
hub's REST API, and this package imports nothing from the hub's own packages.
See cli/README.md.
"""

# Pinned per release with pyproject.toml by scripts/release/bump-version.py, so
# it names the code that is running rather than whichever copy is installed.
__version__ = "0.9.1"
