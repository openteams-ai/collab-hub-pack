"""Command line entry point: ``python -m collab_hub_api.identity_rewrite``.

Configuration comes from the **same environment variables the API pod already
has**, exactly as the inventory does, so the documented way to run this is
inside the deployment being migrated with no new configuration to get wrong.

Exit codes gate a runbook step:

- ``0`` — planned (dry run) or applied, with no errors.
- ``1`` — the report was refused, or a waiver is missing. Nothing was written.
- ``2`` — the run failed partway. **The manifest is still written**, and it is
  the record of what had already changed.

The manifest destination is reserved *before* the first mutation, not written at
the end: an unsafe or unwritable path discovered after the data has changed
leaves a migration with no record at all.

**The manifest is a diagnostic and targeted-reversal aid, not the authoritative
rollback.** It is written at startup and at termination, so a run killed abruptly
can leave committed writes unrecorded. After an interrupted run, re-read the
store with the read-only inventory and fall back to the snapshot — which is why
the runbook takes one first.

The default is a dry run; ``--apply`` is the only way to write.

**Quiescing the deployment is the operator's job, and this tool cannot check
it.** Nothing here detects a running API, so the runbook scales it to zero
first: a write that lands mid-rewrite is recorded under a legacy principal that
no longer exists anywhere else, and no exit code will tell you it happened.
"""

from __future__ import annotations

import argparse
import os
import sys

from .plan import PlanRefusedError, build_plan, load_inventory
from .writers import (
    Manifest,
    rewrite_local_sidecars,
    rewrite_postgres,
    rewrite_s3_sidecars,
    write_private_file,
)

ENV_PREFIX = "COLLAB_HUB_API__"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m collab_hub_api.identity_rewrite",
        description=(
            "Rewrite legacy ACL principals to OIDC subjects, from a reviewed identity "
            "inventory report. Dry run unless --apply is given."
        ),
    )
    parser.add_argument("--inventory", required=True, help="Path to the inventory report written by --json-output.")
    parser.add_argument("--apply", action="store_true", help="Perform the writes. Without it, nothing is written.")
    parser.add_argument(
        "--acknowledge",
        action="append",
        default=[],
        metavar="CLASS",
        help=(
            "Proceed despite a finding this tool will not resolve (ambiguous, "
            "reassignment_suspected, padded, blocking_orphan). Repeatable. None of them is "
            "rewritten either way."
        ),
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="Where to write the change manifest (mode 0600). Required with --apply.",
    )
    parser.add_argument(
        "--frames-backend",
        choices=("auto", "local", "s3", "none"),
        default="auto",
        help="Sidecar backend to rewrite. 'auto' follows FRAMES__STORAGE_BACKEND.",
    )
    parser.add_argument("--skip-postgres", action="store_true", help="Do not touch any Postgres carrier.")
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        metavar="JSON_PATH",
        help=(
            "Substitute at this JSON path even though its key is not identity-bearing, e.g. "
            "'$.some_field'. Repeatable. Without it such a match is reported and left alone."
        ),
    )
    return parser


def _summarise(plan, manifest: Manifest, *, applied: bool) -> str:
    lines = [
        "# Identity rewrite — " + ("APPLIED" if applied else "DRY RUN"),
        "",
        f"- Report generated: {plan.source_generated_at or 'unknown'} (verdict: {plan.source_verdict or 'unknown'})",
        f"- Principals to rewrite: {len(plan.mappings)}",
        f"- Principals left alone: {len(plan.skipped)}",
        f"- Acknowledged findings: {', '.join(plan.acknowledged) or 'none'}",
        f"- Changes {'committed' if applied else 'planned'}: "
        f"{len(manifest.committed) if applied else len(manifest.changes)}",
        "",
    ]
    for note in plan.notes:
        lines.append(f"NOTE: {note}")
    for note in manifest.skipped_carriers:
        lines.append(f"NOT REWRITTEN: {note}")
    for note in manifest.merges:
        lines.append(f"MERGE: {note}")
    if manifest.unexpected_paths:
        lines.append("")
        lines.append(f"## {len(manifest.unexpected_paths)} match(es) NOT substituted: no identity belongs at that path")
        for item in manifest.unexpected_paths[:20]:
            lines.append(f"- {item.carrier} {item.entity} {item.path}")
        if len(manifest.unexpected_paths) > 20:
            lines.append(f"- ... and {len(manifest.unexpected_paths) - 20} more (see the manifest)")
    lines.append("")
    lines.append("## Changes by carrier")
    for carrier, count in manifest.counts.items():
        lines.append(f"- {carrier}: {count}")
    if not manifest.counts:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.apply and not args.manifest:
        print(
            "--apply requires --manifest: a rewrite with no record of what it changed cannot be reviewed or reversed.",
            file=sys.stderr,
        )
        return 1

    try:
        payload = load_inventory(args.inventory)
        plan = build_plan(payload, acknowledge=set(args.acknowledge))
    except PlanRefusedError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    mapping = plan.by_principal
    allow_paths = frozenset(args.allow_path)
    manifest = Manifest(applied=args.apply)
    status = 0

    # Reserve the destination before anything mutates. A symlinked or
    # unwritable manifest path found *after* the sidecar phase leaves a changed
    # store with no reversal record — reproduced in review.
    if args.manifest:
        try:
            write_private_file(args.manifest, manifest.to_json())
        except Exception as exc:  # noqa: BLE001
            print(
                f"Cannot write the manifest at {args.manifest} ({type(exc).__name__}: {exc}). "
                "Refusing to start: a rewrite with nowhere to record itself cannot be reversed.",
                file=sys.stderr,
            )
            return 1

    if not mapping:
        print("Nothing to rewrite: the report maps no principal to a subject.", file=sys.stderr)

    try:
        if not args.skip_postgres:
            url = _env("FRAMES__POSTGRES__URL")
            if url:
                import psycopg

                try:
                    with psycopg.connect(url, row_factory=_dict_row()) as conn:
                        rewrite_postgres(conn, mapping, manifest, apply=args.apply, allow_paths=allow_paths)
                        if not args.apply:
                            conn.rollback()
                except BaseException as exc:
                    # The transaction did not commit, so nothing it staged is
                    # true. Demote before re-raising, or the record outlives the
                    # rollback claiming changes the database discarded.
                    manifest.fail_pending(f"transaction rolled back: {type(exc).__name__}: {exc}")
                    raise
                # Only here has the context manager committed.
                manifest.promote_pending()
            else:
                manifest.skipped_carriers.append("postgres: no FRAMES__POSTGRES__URL configured")

        backend = args.frames_backend
        if backend == "auto":
            backend = _env("FRAMES__STORAGE_BACKEND", "local").strip().lower() or "local"
        if backend == "s3":
            import boto3

            bucket = _env("FRAMES__S3__BUCKET")
            if not bucket:
                manifest.skipped_carriers.append("frame.sidecar: no FRAMES__S3__BUCKET configured")
            else:
                s3 = boto3.client(
                    "s3",
                    endpoint_url=_env("FRAMES__S3__ENDPOINT_URL") or None,
                    region_name=_env("FRAMES__S3__REGION") or None,
                )
                prefix = _env("FRAMES__S3__PREFIX", "frames")
                rewrite_s3_sidecars(s3, bucket, prefix, mapping, manifest, apply=args.apply, allow_paths=allow_paths)
        elif backend == "local":
            rewrite_local_sidecars(
                _env("STORAGE__FRAMES_PATH", "/var/frames"),
                mapping,
                manifest,
                apply=args.apply,
                allow_paths=allow_paths,
            )
        elif backend == "none":
            manifest.skipped_carriers.append("frame.sidecar: skipped by --frames-backend=none")
    except Exception as exc:  # noqa: BLE001 - the manifest must survive any failure
        manifest.errors.append(f"{type(exc).__name__}: {exc}")
        status = 2

    if args.manifest:
        write_private_file(args.manifest, manifest.to_json())
        print(f"Wrote {args.manifest} (mode 0600)", file=sys.stderr)
    if args.apply:
        print(
            "The manifest describes this run; it is not a durable journal. If a run is killed "
            "abruptly, re-read the store with the identity inventory rather than trusting the "
            "manifest, and use the pre-migration snapshot as the way back.",
            file=sys.stderr,
        )
    if manifest.rolled_back:
        print(
            "The database transaction rolled back: entries it had staged are recorded as failed, "
            "not committed. Object and file writes are separate and are recorded on their own.",
            file=sys.stderr,
        )

    print(_summarise(plan, manifest, applied=args.apply))
    if manifest.unexpected_paths:
        print(
            f"{len(manifest.unexpected_paths)} value(s) equal to a mapped principal were found where no "
            "identity belongs and were left unchanged. Review them in the manifest; if one really is an "
            "identity, re-run naming it with --allow-path. Nothing at those paths has been rewritten.",
            file=sys.stderr,
        )
        status = status or 1
    if manifest.errors:
        for error in manifest.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if args.apply:
            print(
                "The run stopped partway. The manifest lists what had already changed; re-running "
                "after the cause is fixed is safe — the plan is keyed on legacy principals, so "
                "completed substitutions match nothing the second time.",
                file=sys.stderr,
            )
    return status


def _dict_row():
    from psycopg.rows import dict_row

    return dict_row


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
