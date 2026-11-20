---
type: cog [0.1]
name: hermes
description: Harness Cog. Hermes Agent behind the hub's seam, driven over ACP with no tools, answering one prompt or holding a session; its model comes from the binding the hub delivers.
version: "0.2.0"
license: BSD-3-Clause
publisher: OpenTeams
manifest: pixi.toml
manifest_schema: openteams/cog-manifest [0.1]
---

# Hermes

[Hermes Agent](https://hermes-agent.nousresearch.com) as a Cog: the hub launches it, keeps a session open with it and relays turns to it, without knowing it is Hermes. Its worker is on the [worker SDK](https://github.com/openteams-ai/collab-hub-pack/tree/main/worker), and what is Hermes's own (a home for the run, its configuration from the binding, its environment) is forty lines of adapter.

## Entry points

- `ask` answers one prompt, `{"prompt": "..."}`, with `{"answer": "..."}`.
- `session` opens a Hermes session and holds it: each turn the hub delivers is one prompt, answered with what Hermes said. It ends on `bye`, or when the run is terminated.

## What it requires

A model: any OpenAI-compatible endpoint, or Claude through Hermes's own Anthropic provider. The hub offers its inventory to the Cog's `resolve` at install, and delivers the binding to every worker; the Cog's `check` starts Hermes against it.
