# The result envelope (version 1)

What a Cog's usage entry point returns. This is the hub's profile answer to
two things the Cog specification deliberately leaves to hosting
environments — the payload key and the pass/fail surface — and it belongs
in the hub's capability list, not in the Cog spec. A Cog that emits this
envelope is compatible with the hub's seam; other environments may require
otherwise.

The envelope is requirements-driven: its fields are exactly what Guards
check, Gates read, and Tracks record. Nothing else.

```json
{
  "envelope": 1,
  "cog":      {"id": "openteams/<name>", "version": "0.1.0"},
  "task":     "ask",
  "ok":       true,
  "error":    null,
  "payload":  { ... },
  "raw":      "<verbatim model text>",
  "problems": [
    {"check": "grounding", "detail": "…not a verbatim span…", "severity": "error"}
  ],
  "binding":  {
    "record": {...},
    "model_echoed": "...",
    "model_identity": "verified|unverified|mismatch",
    "violations": [],
    "resolved_from": "model.json"
  },
  "usage":    {"tokens": 1234, "cost": 0.0021},
  "timing":   {"latency_s": 2.31}
}
```

## Field rules

**`envelope`** — the version discriminator for this contract (`1` today).
Consumers detect the envelope version here, not by sniffing field shapes.

**`cog`** / **`task`** — which Cog and which usage entry point produced this
result.

**`payload`** — the fixed key for the Cog's output. Its shape is the Cog's
own declared output schema; `null` when the run errored or the output did
not parse.

**`ok`** — transport-level success: the invocation completed and produced
a parseable payload. **`ok: true` may coexist with a non-empty `problems`
list.** "Passed with integrity problems" is deliberately legible, and
deciding what to do about it is a Gate's job (human or reviewer Cog) —
never the Cog's.

**`error`** — `null`, or `{code, detail}`. Codes: `binding-invalid`,
`invalid-input`, `model-unavailable`, `model-call-failed`,
`model-response-malformed`, `context-exhausted`. When set, `ok` is `false`.
Over HTTP, the status maps 4xx/5xx accordingly: 422 for `invalid-input` and
`context-exhausted`, 502 for an upstream fault, 503 for unavailable/binding.

`context-exhausted` is the input that did not fit: the prompt and the step's
Frames could not fit the bound model's context window, even after the
harness's own recovery, or the model refused them as too long. Retrying on the
same binding would not help, so a Gate or a person chooses a larger model or
fewer Frames; a worker reports it before spending anything when it can tell.

**`problems`** — the Cog's self-reported **contract-check** findings,
structured for Guards and Gates to consume: `check` (machine-readable
category — `schema`, `grounding`, `citation`, `identity`, `input`, …),
`detail` (a human sentence), `severity` (`error` | `warn`). These come from
the Cog checking its *own* declared contract. An independent Guard verifies
against the *system's* requirements and never treats this self-report as
its verdict.

**`binding`** — the Track fields: the complete binding identity copied into
every result, so a saved result identifies its run (which pinned model,
which endpoint, identity verdict, violations) without reading current
installation state. This is what makes a Gate signature auditable.

**`raw`** — the verbatim model text, for audit and salvage review.

**`usage`** — tokens and cost attributable to this interaction, at the
envelope level (not inside `payload`, which is the Cog's own output).
Optional; absent when the Cog cannot report it.

**`timing`** — wall-clock latency of the interaction.

## What the hub does with it

- The **Track** records the envelope's `binding`, `problems`, `usage`, and
  a reference to `payload` for each step.
- **Guards** run on `payload` and read `problems` as input.
- The **Gate** declared on the Op step consumes `ok`, `problems`, and Guard
  findings and decides ok / ok-with-problems / escalate. A pause for human
  review is the Gate's outcome, not something the Cog requests: by default a
  problem with severity `error` escalates, and a `warn` is recorded and passes.
- **Budgets** consume `usage`.

## Over HTTP

As the hub's worker client implements it (`collab_hub_execution.kubernetes`):

| Status | Body | The hub reads |
|---|---|---|
| 200 | an envelope, `ok` true or false | the envelope |
| 422 | an envelope with `error.code: invalid-input` or `context-exhausted` | the envelope's code |
| 502 | an envelope with `model-call-failed` or `model-response-malformed` | the envelope's code |
| 503 | an envelope with `model-unavailable` or `binding-invalid` | the envelope's code |
| 422 / 502 / 503 | not an envelope — a proxy's error page, a worker that died mid-answer | an error envelope with the status's own code: `invalid-input`, `model-call-failed`, `model-unavailable` |
| anything else | — | a transport error; the step fails by the exception's name, not a code |

A 200 whose body is not an envelope, and an error status carrying `ok: true`,
are invalid answers (`EnvelopeInvalid`) and fail the step as such. The `usage`
of an `ok: false` envelope is still recorded, so a failed call's spending
counts against the budget.

## Versioning

This document is `envelope: 1`. Additive changes (new optional fields) do
not bump the version; changing the meaning or type of an existing field
does. Consumers must ignore unknown fields.
