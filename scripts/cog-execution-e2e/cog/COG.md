---
type: cog [0.1]
name: echo
description: The kind E2E's fake Cog. Echoes its input and reports the binding it was delivered.
version: "0.1.0"
manifest: pixi.toml
manifest_schema: openteams/cog-manifest [0.1]
---

# echo

A fake Cog for the kind end-to-end: its worker answers `run` with its input, the findings a send back delivered, and the binding file the hub mounted, so the test can assert the pod was configured from its delivered binding alone.
