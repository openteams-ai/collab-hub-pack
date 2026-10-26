"""The reconciliation indexer (issue #84) against the static adapter over a fake OCI client.

Every scenario the issue names: a new artifact, an unchanged artifact (second
sweep changes nothing), a retagged artifact (written without refetching), a
removed artifact (marked, never deleted, still readable by digest), a reader
failure recorded while the sweep continues, non-Cog artifacts recorded once
with a reason, single flight under the sweep lock, and the targeted entry
points a webhook receiver will call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cog_registry_fakes import HOST, URL, FakeOCIFactory, manifest_for

from collab_hub_api.cogs.adapters.static import MAX_TAGS_PER_REPOSITORY
from collab_hub_api.cogs.bundle import COG_ENTRY_FILE, read_cog_bundle
from collab_hub_api.cogs.catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
    CogCatalogDataError,
    InMemoryCogCatalogStore,
    UnavailableCogCatalogStore,
)
from collab_hub_api.cogs.indexer import (
    MAX_TITLES_IN_REASON,
    CogIndexer,
    SweepSummary,
)
from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_NEBI_ASSET,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_LOCK,
    MEDIA_TYPE_PIXI_TOML,
    TITLE_ANNOTATION,
    Descriptor,
    OCIError,
)
from collab_hub_api.cogs.registry import (
    ArtifactRef,
    CogRegistrySourceConfig,
    RegistrySourceError,
    build_registry_sources,
)
from collab_hub_api.config import Config
from collab_hub_api.frames.observability import COG_INDEX_ARTIFACTS, COG_INDEX_SWEEPS

FIXTURES = Path(__file__).parent / "fixtures" / "cogs"
SOURCE_ID = "static-main"
PUSHED = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def blob_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layers_for(files: dict[str, bytes], *, lockfile: bytes | None = None) -> tuple[tuple[Descriptor, ...], dict]:
    """OCI layer descriptors (titled) and the blob map for a bundle's files."""

    descriptors = []
    blobs = {}
    for title, data in files.items():
        media_type = MEDIA_TYPE_PIXI_TOML if title == "pixi.toml" else MEDIA_TYPE_NEBI_ASSET
        descriptors.append(
            Descriptor(
                media_type=media_type, digest=blob_digest(data), size=len(data), annotations={TITLE_ANNOTATION: title}
            )
        )
        blobs[blob_digest(data)] = data
    if lockfile is not None:
        descriptors.append(
            Descriptor(
                media_type=MEDIA_TYPE_PIXI_LOCK,
                digest=blob_digest(lockfile),
                size=len(lockfile),
                annotations={TITLE_ANNOTATION: "pixi.lock"},
            )
        )
        blobs[blob_digest(lockfile)] = lockfile
    return tuple(descriptors), blobs


def fixture_files(name: str) -> dict[str, bytes]:
    root = FIXTURES / name
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"bundle-paths.txt", "expected-card.json"}
    }


def entry(
    digest_seed: str,
    tags: list[str],
    files: dict[str, bytes],
    *,
    pushed_at: datetime | None = PUSHED,
    lockfile: bytes | None = None,
    extra_layers: tuple[Descriptor, ...] = (),
) -> dict:
    layers, blobs = layers_for(files, lockfile=lockfile)
    return {
        "digest": "sha256:" + (digest_seed * 64)[:64],
        "tags": tags,
        "pushed_at": pushed_at,
        "media_type": MEDIA_TYPE_OCI_MANIFEST,
        "layers": layers + extra_layers,
        "blobs": blobs,
    }


COMPLETE = fixture_files("pixi-complete")
CONTEXT = fixture_files("pixi-context")
PROG = fixture_files("prog")
DRAFT = fixture_files("draft")


def registry() -> dict[str, list[dict]]:
    return {
        "cogs/transcriber": [entry("a", ["v1", "latest"], COMPLETE, lockfile=b"lock: 1\n")],
        "cogs/notes": [entry("b", ["v1"], CONTEXT)],
    }


def make(artifacts: dict[str, list[dict]] | None = None, **indexer_kwargs):
    """A static source over a fake OCI client, an in-memory store, and an indexer over both."""

    factory = FakeOCIFactory(artifacts if artifacts is not None else registry())
    config = CogRegistrySourceConfig(id=SOURCE_ID, kind="static", url=URL, repositories=sorted(factory.artifacts))
    (source,) = build_registry_sources([config], oci_client_factory=factory)
    store = InMemoryCogCatalogStore()
    indexer = CogIndexer(store, [source], **indexer_kwargs)
    return indexer, store, factory.clients[0]


def counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def blob_calls(client) -> list[tuple[str, ...]]:
    return [call for call in client.calls if call[0] == "get_blob"]


def manifest_by_digest_calls(client) -> list[tuple[str, ...]]:
    return [call for call in client.calls if call[0] == "get_manifest" and call[2].startswith("sha256:")]


# ---------------------------------------------------------------------------
# One sweep
# ---------------------------------------------------------------------------


async def test_first_sweep_indexes_every_cog_with_the_readers_card():
    indexer, store, client = make()
    before = counter(COG_INDEX_ARTIFACTS, outcome="indexed")

    summary = await indexer.sweep()

    assert (summary.indexed, summary.skipped, summary.failed, summary.removed, summary.non_cog) == (2, 0, 0, 0, 0)
    assert summary.sources == 1 and summary.sources_failed == 0 and not summary.locked_out
    assert counter(COG_INDEX_ARTIFACTS, outcome="indexed") == before + 2
    assert indexer.last_summary is summary

    transcriber = store.get("sha256:" + "a" * 64)
    assert transcriber.status == STATUS_INDEXED
    # Acceptance: the stored card IS the reader's output for the same files
    # and layer titles (the lockfile is a title but never a fetched file).
    expected = read_cog_bundle(COMPLETE, bundle_paths=[*COMPLETE, "pixi.lock"]).to_dict()
    assert transcriber.card == expected
    assert transcriber.cog_id == "example/cog-audio-transcriber"
    assert transcriber.name == "cog-audio-transcriber" and transcriber.version == "0.1.0"
    assert transcriber.kind == "complete" and transcriber.publisher == "Example Organization"
    assert transcriber.manifest_schema == "openteams/cog-manifest [0.1]"
    assert transcriber.tags == ("latest", "v1") and transcriber.pushed_at == PUSHED
    assert transcriber.manifest_media_type == MEDIA_TYPE_OCI_MANIFEST
    assert transcriber.reference == f"{HOST}/cogs/transcriber@sha256:{'a' * 64}"
    assert transcriber.read_errors == ()

    # Fetch strategy: COG.md first, then only the profile file it names. The
    # lockfile is never fetched.
    fetched = {digest for _, repo, digest in blob_calls(client) if repo == "cogs/transcriber"}
    assert fetched == {blob_digest(COMPLETE[COG_ENTRY_FILE]), blob_digest(COMPLETE["pixi.toml"])}
    assert blob_digest(b"lock: 1\n") not in {call[2] for call in blob_calls(client)}

    notes = store.get("sha256:" + "b" * 64)
    assert notes.cog_id == "example/cog-meeting-notes" and notes.kind == "context"
    assert {row.cog_id for row in store.list_current()} == {
        "example/cog-audio-transcriber",
        "example/cog-meeting-notes",
    }


async def test_second_sweep_changes_nothing_and_fetches_no_bundles():
    indexer, store, client = make()
    await indexer.sweep()
    rows_before = {row.digest: row for row in (store.get("sha256:" + "a" * 64), store.get("sha256:" + "b" * 64))}
    client.calls.clear()

    summary = await indexer.sweep()

    assert (summary.indexed, summary.skipped, summary.retagged, summary.removed) == (0, 2, 0, 0)
    assert blob_calls(client) == [], "an unchanged digest costs no blob fetch"
    assert manifest_by_digest_calls(client) == [], "and no manifest fetch by digest"
    for digest, before in rows_before.items():
        assert store.get(digest) == before


async def test_retagged_artifact_is_updated_without_refetching():
    artifacts = registry()
    indexer, store, client = make(artifacts)
    await indexer.sweep()
    client.calls.clear()

    # Move `latest` from the transcriber to a new tag set: the fake's tag
    # table is what list_tags answers, so rewrite it.
    client.tags["cogs/transcriber"] = ["v1", "stable"]
    manifest = manifest_for(artifacts["cogs/transcriber"][0])
    client.manifests[("cogs/transcriber", "stable")] = manifest
    del client.manifests[("cogs/transcriber", "latest")]

    summary = await indexer.sweep()

    assert (summary.retagged, summary.skipped, summary.indexed) == (1, 1, 0)
    row = store.get("sha256:" + "a" * 64)
    assert row.tags == ("stable", "v1")
    assert row.card is not None and row.status == STATUS_INDEXED
    assert blob_calls(client) == []
    assert manifest_by_digest_calls(client) == []


async def test_removed_artifact_is_marked_not_deleted_and_stays_readable():
    indexer, store, client = make()
    await indexer.sweep()

    # Delete the notes Cog from the registry.
    client.tags["cogs/notes"] = []
    del client.manifests[("cogs/notes", "v1")]

    summary = await indexer.sweep()

    assert summary.removed == 1 and summary.skipped == 1
    notes = store.get("sha256:" + "b" * 64)
    assert notes is not None and notes.removed_at is not None
    assert notes.card is not None and notes.cog_id == "example/cog-meeting-notes"
    assert [row.cog_id for row in store.list_current()] == ["example/cog-audio-transcriber"]
    assert store.list_versions("example/cog-meeting-notes") == []
    assert len(store.list_versions("example/cog-meeting-notes", include_removed=True)) == 1

    # A third sweep does not count it again ...
    assert (await indexer.sweep()).removed == 0

    # ... and if the digest comes back it is restored without a refetch.
    client.tags["cogs/notes"] = ["v1"]
    client.manifests[("cogs/notes", "v1")] = client.manifests[("cogs/notes", "sha256:" + "b" * 64)]
    client.calls.clear()
    summary = await indexer.sweep()
    assert summary.retagged == 1 and blob_calls(client) == []
    assert store.get("sha256:" + "b" * 64).present


async def test_fetch_failure_is_recorded_and_the_sweep_continues_then_retries():
    artifacts = registry()
    indexer, store, client = make(artifacts)
    # The transcriber's COG.md blob is missing from the registry.
    broken = blob_digest(COMPLETE[COG_ENTRY_FILE])
    del client.blobs[broken]

    summary = await indexer.sweep()

    assert (summary.failed, summary.indexed) == (1, 1)
    row = store.get("sha256:" + "a" * 64)
    assert row.status == STATUS_FAILED and row.card is None
    assert row.read_errors == (f"fetch: OCINotFound: {broken}",)
    assert row.tags == ("latest", "v1") and row.reference.endswith("@sha256:" + "a" * 64)
    assert store.get("sha256:" + "b" * 64).status == STATUS_INDEXED
    assert store.list_current() and all(r.status == STATUS_INDEXED for r in store.list_current())

    # A failed row is retried on the next sweep -- and indexes once the blob is back.
    client.blobs[broken] = COMPLETE[COG_ENTRY_FILE]
    summary = await indexer.sweep()
    assert (summary.indexed, summary.skipped, summary.failed) == (1, 1, 0)
    assert store.get("sha256:" + "a" * 64).status == STATUS_INDEXED


async def test_oversized_bundle_file_is_a_recorded_failure():
    indexer, store, _ = make(max_bytes_per_file=16)

    summary = await indexer.sweep()

    assert summary.failed == 2 and summary.indexed == 0
    row = store.get("sha256:" + "a" * 64)
    assert row.status == STATUS_FAILED
    assert row.read_errors[0].startswith("fetch: OCITooLarge")


async def test_cards_with_errors_are_still_indexed_with_their_errors_mirrored():
    indexer, store, _ = make({"cogs/draft": [entry("d", ["v0"], DRAFT)]})

    summary = await indexer.sweep()

    assert summary.indexed == 1
    row = store.get("sha256:" + "d" * 64)
    expected = read_cog_bundle(DRAFT, bundle_paths=list(DRAFT))
    assert row.status == STATUS_INDEXED
    assert row.card == expected.to_dict()
    assert list(row.read_errors) == expected.errors
    # A draft declares no profile, so it has no id and is not in the current list.
    assert row.cog_id is None and store.list_current() == []


async def test_non_cog_artifacts_are_recorded_once_with_a_reason():
    image_layers = tuple(
        Descriptor(media_type="application/vnd.oci.image.layer.v1.tar+gzip", digest="sha256:" + f"{i:064x}", size=10)
        for i in range(3)
    )
    titled = tuple(
        Descriptor(
            media_type=MEDIA_TYPE_NEBI_ASSET,
            digest="sha256:" + f"{i + 100:064x}",
            size=1,
            annotations={TITLE_ANNOTATION: f"file-{i}.bin"},
        )
        for i in range(MAX_TITLES_IN_REASON + 2)
    )
    indexer, store, client = make(
        {
            "images/nginx": [entry("e", ["1.27"], {}, extra_layers=image_layers)],
            "images/tools": [entry("f", ["v2"], {}, extra_layers=titled)],
        }
    )

    summary = await indexer.sweep()

    assert (summary.non_cog, summary.indexed, summary.failed) == (2, 0, 0)
    nginx = store.get("sha256:" + "e" * 64)
    assert nginx.status == STATUS_NON_COG and nginx.card is None
    assert nginx.read_errors == ("manifest carries no COG.md or pixi.toml layer; 3 layer(s), none titled",)
    tools = store.get("sha256:" + "f" * 64)
    assert tools.read_errors[0].endswith("file-7.bin and 2 more")
    assert blob_calls(client) == [], "nothing is fetched from a manifest with no bundle files"
    assert store.list_current() == []

    # The second sweep skips them: no manifest fetch by digest, nothing re-read.
    client.calls.clear()
    summary = await indexer.sweep()
    assert summary.skipped == 2 and summary.non_cog == 0
    assert manifest_by_digest_calls(client) == [] and blob_calls(client) == []


async def test_prog_without_cog_md_is_indexed_and_pixi_without_capability_is_non_cog():
    indexer, store, client = make(
        {
            "progs/local-server": [entry("p", ["v1"], PROG)],
            "images/pixi-only": [entry("q", ["v1"], {"pixi.toml": b'[workspace]\nname = "x"\n'})],
        }
    )

    summary = await indexer.sweep()

    assert (summary.indexed, summary.non_cog) == (1, 1)
    prog = store.get("sha256:" + "p" * 64)
    assert prog.status == STATUS_INDEXED and prog.kind == "prog" and prog.cog_id == "example/local-server"
    assert prog.card == read_cog_bundle(PROG, bundle_paths=list(PROG)).to_dict()
    plain = store.get("sha256:" + "q" * 64)
    assert plain.status == STATUS_NON_COG
    assert plain.read_errors == ("bundle has no COG.md and pixi.toml declares no [tool.nebi.capability]",)
    # Both fetched exactly one file: pixi.toml.
    assert len(blob_calls(client)) == 2


# ---------------------------------------------------------------------------
# Enumeration failures never mark a catalog gone
# ---------------------------------------------------------------------------


class FlakySource:
    """A RegistrySource whose enumeration fails on demand, wrapping a real one."""

    def __init__(self, inner, *, fail_repositories: bool = False, fail_repo: str | None = None):
        self.inner = inner
        self.id = inner.id
        self.host = inner.host
        self.fail_repositories = fail_repositories
        self.fail_repo = fail_repo

    async def list_repositories(self):
        if self.fail_repositories:
            raise RegistrySourceError("listing API answered HTTP 502")
        return await self.inner.list_repositories()

    async def list_artifacts(self, repo):
        if repo == self.fail_repo:
            raise OCIError("connection reset")
        return await self.inner.list_artifacts(repo)

    def oci(self):
        return self.inner.oci()

    def parse_event(self, request):
        return None

    async def aclose(self):
        await self.inner.aclose()


async def test_source_enumeration_failure_skips_removal_and_continues():
    indexer, store, client = make()
    await indexer.sweep()
    (real,) = indexer.sources

    flaky = FlakySource(real, fail_repositories=True)
    broken = CogIndexer(store, [flaky])
    # Meanwhile the registry lost everything -- which the indexer cannot see.
    client.tags = {}
    client.manifests = {}

    summary = await broken.sweep()

    assert summary.sources_failed == 1 and summary.removed == 0
    # Class name only: adapter error messages may quote configured URLs.
    assert summary.errors == [f"{SOURCE_ID}: list_repositories: RegistrySourceError"]
    assert all(row.present for row in store.list_current()) and len(store.list_current()) == 2


async def test_one_repository_failing_reconciles_the_rest_but_skips_removal():
    artifacts = registry()
    indexer, store, client = make(artifacts)
    await indexer.sweep()
    (real,) = indexer.sources

    # Notes is gone from the registry, but the transcriber listing fails:
    # the sweep must not conclude that notes was removed.
    client.tags["cogs/notes"] = []
    del client.manifests[("cogs/notes", "v1")]
    artifacts["cogs/transcriber"].append(entry("c", ["v2"], COMPLETE))
    client.seed({"cogs/transcriber": artifacts["cogs/transcriber"][-1:]})
    flaky = FlakySource(real, fail_repo="cogs/notes")

    summary = await CogIndexer(store, [flaky]).sweep()

    assert summary.sources_failed == 1 and summary.removed == 0
    assert summary.indexed == 1, "the healthy repository was still reconciled"
    assert store.get("sha256:" + "b" * 64).present
    assert store.get("sha256:" + "c" * 64).status == STATUS_INDEXED


def stale(repository: str, seed: str) -> CogArtifact:
    """A present row for this source whose digest the registry no longer has."""

    return CogArtifact(
        source_id=SOURCE_ID,
        host=HOST,
        repository=repository,
        digest="sha256:" + (seed * 64)[:64],
        status=STATUS_INDEXED,
        tags=("old",),
        pushed_at=PUSHED,
    )


async def test_over_bound_repository_shields_only_its_own_rows_from_removal():
    # The per-repository bound is a refusal, never a prefix, and its blast
    # radius is that one repository: everything sorted BEHIND it still
    # reconciles -- including removal -- while the over-bound repository's
    # own rows are left untouched, because what was not enumerated cannot be
    # declared gone (round-3 codex finding: a permanently oversized
    # repository used to disable removal for its whole source, forever).
    artifacts = {
        # Sorted first, and over a bound of one.
        "cogs/a-big": [entry("a", ["v1"], COMPLETE), entry("c", ["v2"], COMPLETE)],
        # Sorted after it: an implementation that stopped at the over-bound
        # repository would never reach this one.
        "cogs/z-small": [entry("b", ["v1"], CONTEXT)],
    }
    indexer, store, _ = make(artifacts, max_artifacts_per_repository=1)
    store.upsert(stale("cogs/a-big", "1"))  # must survive: its repository was not enumerated
    store.upsert(stale("cogs/z-small", "2"))  # must be removed: its repository was fully listed
    store.upsert(stale("cogs/vanished", "3"))  # must be removed: the source no longer lists that repository

    for sweep in range(2):  # the same outcome every sweep: no prefix creep
        summary = await indexer.sweep()
        assert summary.sources_failed == 1
        assert summary.errors == [
            f"{SOURCE_ID}: list_artifacts cogs/a-big: over the 1-artifact bound; removal skipped for it"
        ]
        assert summary.removed == (2 if sweep == 0 else 0)

    assert store.get("sha256:" + "b" * 64).status == STATUS_INDEXED, "the repository behind the bound was reached"
    assert store.get("sha256:" + "a" * 64) is None and store.get("sha256:" + "c" * 64) is None
    assert store.get("sha256:" + "1" * 64).present, "rows of the over-bound repository are not declared gone"
    assert not store.get("sha256:" + "2" * 64).present
    assert not store.get("sha256:" + "3" * 64).present


async def test_retries_never_starve_new_artifacts_of_the_budget():
    # Round-2 codex repro: with budget 1, a permanently failing artifact
    # sorted first used to eat the whole budget every sweep, so the healthy
    # artifact behind it never indexed. Never-seen artifacts now outrank
    # retries: the poison is fetched (and fails) on sweep 1, the valid one is
    # fetched on sweep 2, and from then on the budget goes to the retry.
    poison = dict(COMPLETE)
    poison[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\x00tail"
    indexer, store, _ = make(
        {
            # "cogs/a..." sorts before "cogs/b...": the poison is the prefix.
            "cogs/a-poison": [entry("e", ["v1"], poison)],
            "cogs/b-valid": [entry("b", ["v1"], CONTEXT)],
        },
        max_new_fetches_per_sweep=1,
    )

    first = await indexer.sweep()
    assert (first.failed, first.deferred, first.indexed) == (1, 1, 0)

    second = await indexer.sweep()
    assert (second.indexed, second.failed, second.deferred) == (1, 0, 1), "the valid artifact landed on sweep 2"
    assert store.get("sha256:" + "b" * 64).status == STATUS_INDEXED

    third = await indexer.sweep()
    assert (third.skipped, third.failed, third.deferred) == (1, 1, 0), "the leftover budget now retries the poison"


async def test_retries_get_a_reserved_share_under_a_continuous_unseen_backlog():
    # Round-3 codex finding: strict never-seen-first let a source that always
    # has more new artifacts than the budget starve every retry forever. The
    # schedule now deals every fourth slot to a retry and advances its phase
    # each sweep, so even at budget 1 the retry is reached within four sweeps
    # while new artifacts keep arriving.
    poison = dict(COMPLETE)
    poison[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\x00tail"
    artifacts = {"cogs/a-poison": [entry("e", ["v1"], poison)], "cogs/b-stream": [entry("0", ["v1"], CONTEXT)]}
    indexer, store, client = make(artifacts, max_new_fetches_per_sweep=2)
    first = await indexer.sweep()
    assert (first.failed, first.indexed) == (1, 1)  # the poison is now a failed row

    indexer._max_new_fetches = 1
    retried = 0
    for n in range(1, 5):
        # A brand-new artifact every sweep: the unseen backlog never drains.
        new = entry(str(n), ["v1"], CONTEXT)
        artifacts["cogs/b-stream"].append(new)
        client.seed({"cogs/b-stream": [new]})
        summary = await indexer.sweep()
        assert summary.deferred == 1, "budget 1, two candidates: exactly one waits each sweep"
        retried += summary.failed
    assert retried == 1, "the failed row was retried once within a four-sweep window despite constant arrivals"
    assert store.get("sha256:" + "1" * 64).status == STATUS_INDEXED, "and new artifacts kept landing"


async def test_retries_rotate_so_a_prefix_of_permanent_failures_does_not_starve_a_recoverable_one():
    # Three failed rows and a budget of one: without a cursor the same first
    # failure would be retried every sweep and the third never. With the
    # rotating cursor every failed row is retried once across three sweeps.
    poison = dict(COMPLETE)
    poison[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\x00tail"
    artifacts = {f"cogs/{name}": [entry(seed, ["v1"], poison)] for name, seed in (("a", "1"), ("b", "2"), ("c", "3"))}
    indexer, store, client = make(artifacts, max_new_fetches_per_sweep=3)
    assert (await indexer.sweep()).failed == 3

    # The third one recovers: its publisher fixed the bundle under the same digest
    # is impossible, so model recovery as the registry now serving a healthy
    # manifest for that digest (what a transient 5xx-then-fine looks like).
    fixed = entry("3", ["v1"], CONTEXT)
    client.seed({"cogs/c": [fixed]})
    indexer._max_new_fetches = 1
    outcomes = []
    for _ in range(3):
        summary = await indexer.sweep()
        outcomes.append((summary.indexed, summary.failed, summary.deferred))
    assert sorted(outcomes) == [(0, 1, 2), (0, 1, 2), (1, 0, 2)], "each failed row got exactly one retry"
    assert store.get("sha256:" + "3" * 64).status == STATUS_INDEXED


async def test_missing_configured_repository_is_empty_not_failed():
    indexer, store, client = make()
    await indexer.sweep()
    # The registry no longer has the notes repository at all (404 on tags).
    del client.tags["cogs/notes"]

    summary = await indexer.sweep()

    assert summary.sources_failed == 0 and summary.removed == 1
    assert store.get("sha256:" + "b" * 64).removed_at is not None


# ---------------------------------------------------------------------------
# Single flight
# ---------------------------------------------------------------------------


async def test_sweep_is_single_flight_under_the_store_lock():
    indexer, store, _ = make()
    before = counter(COG_INDEX_SWEEPS, result="locked_out")

    with store.sweep_lock() as held:
        assert held
        summary = await indexer.sweep()

    assert summary.locked_out is True
    assert summary.indexed == 0 and store.list_current() == []
    assert counter(COG_INDEX_SWEEPS, result="locked_out") == before + 1

    # Released: the next sweep proceeds and the lock is free afterwards.
    assert (await indexer.sweep()).indexed == 2
    with store.sweep_lock() as free:
        assert free


async def test_two_indexers_starting_together_do_not_double_index():
    indexer, store, client = make()
    other = CogIndexer(store, indexer.sources)

    first, second = await asyncio.gather(indexer.sweep(), other.sweep())

    winners = [s for s in (first, second) if not s.locked_out]
    assert len(winners) == 1 and winners[0].indexed == 2
    # Each Cog's bundle files were fetched once.
    assert len(blob_calls(client)) == 4


async def test_lock_is_released_when_a_sweep_raises():
    indexer, store, _ = make()

    class Boom(Exception):
        pass

    async def explode(*args, **kwargs):
        raise Boom()

    indexer._sweep_source = explode  # type: ignore[method-assign]
    before = counter(COG_INDEX_SWEEPS, result="failed")
    with pytest.raises(Boom):
        await indexer.sweep()
    assert counter(COG_INDEX_SWEEPS, result="failed") == before + 1
    with store.sweep_lock() as free:
        assert free
    assert indexer.last_summary is not None and indexer.last_summary.duration_seconds >= 0


# ---------------------------------------------------------------------------
# Targeted entry points (webhook receiver, #86)
# ---------------------------------------------------------------------------


async def test_reindex_writes_one_artifact_regardless_of_catalog_state():
    indexer, store, client = make()

    row = await indexer.reindex(SOURCE_ID, "cogs/notes", "sha256:" + "b" * 64, tags=["v1", "hot"], pushed_at=PUSHED)

    assert isinstance(row, CogArtifact) and row.status == STATUS_INDEXED
    assert row.tags == ("hot", "v1") and row.cog_id == "example/cog-meeting-notes"
    assert store.get("sha256:" + "b" * 64) == store.get(row.digest)
    assert store.get(row.digest).card == row.card

    # A digest the registry does not have is recorded as failed, not raised.
    row = await indexer.reindex(SOURCE_ID, "cogs/notes", "sha256:" + "9" * 64)
    assert row.status == STATUS_FAILED and row.read_errors[0].startswith("fetch: OCINotFound")

    with pytest.raises(KeyError):
        await indexer.reindex("no-such-source", "cogs/notes", "sha256:" + "b" * 64)


async def test_mark_removed_marks_one_row():
    indexer, store, _ = make()
    await indexer.sweep()
    before = counter(COG_INDEX_ARTIFACTS, outcome="removed")

    assert await indexer.mark_removed(SOURCE_ID, "cogs/notes", "sha256:" + "b" * 64) is True
    assert await indexer.mark_removed(SOURCE_ID, "cogs/notes", "sha256:" + "b" * 64) is False
    assert counter(COG_INDEX_ARTIFACTS, outcome="removed") == before + 1
    assert store.get("sha256:" + "b" * 64).removed_at is not None
    with pytest.raises(KeyError):
        await indexer.mark_removed("no-such-source", "cogs/notes", "sha256:" + "b" * 64)


async def test_the_sweep_writes_through_the_locks_view_and_reindex_through_the_store():
    # Issues #128 and #148: the sweep's reads and writes go through the view
    # its lock returned (on Postgres, the lock's own session) and never
    # through the store; the lock-less reindex writes through the store --
    # the fallback row for an unstorable card included -- and never through
    # a view, because it holds none. The store below hands out a view that
    # is a distinct object, so the two routes are told apart by identity.
    routes: list[str] = []

    class RecordingStore(InMemoryCogCatalogStore):
        def upsert(self, artifact, route="store"):
            routes.append(route)
            if artifact.card is not None and artifact.repository == "cogs/transcriber":
                raise CogCatalogDataError("DataError")
            super().upsert(artifact)

        @contextmanager
        def sweep_lock(self):
            with super().sweep_lock() as held:
                yield _RecordingView(self) if held is not None else None

    class _RecordingView:
        def __init__(self, store):
            self._store = store

        def known(self, source_id):
            routes.append("view:known")
            return self._store.known(source_id)

        def upsert(self, artifact):
            self._store.upsert(artifact, route="view")

        def update_tags(self, *args, **kwargs):
            routes.append("view:update_tags")
            return self._store.update_tags(*args, **kwargs)

        def mark_removed(self, *args, **kwargs):
            routes.append("view:mark_removed")
            return self._store.mark_removed(*args, **kwargs)

    base, _, _ = make()
    indexer = CogIndexer(RecordingStore(), base.sources)

    await indexer.reindex(SOURCE_ID, "cogs/transcriber", "sha256:" + "a" * 64)
    assert routes == ["store", "store"], "the reindex upsert and its failed-row fallback both go through the store"

    routes.clear()
    summary = await indexer.sweep()
    assert summary.indexed + summary.failed == 2
    assert routes and all(route.startswith("view") for route in routes), routes
    assert {"view:known", "view", "view:mark_removed"} <= set(routes), "every sweep-path call went through the view"

    # A retag on the next sweep is a view write too.
    client = base.sources[0].oci()
    client.tags["cogs/notes"] = ["v1", "stable"]
    client.manifests[("cogs/notes", "stable")] = client.manifests[("cogs/notes", "v1")]
    routes.clear()
    summary = await indexer.sweep()
    assert summary.retagged == 1
    assert "view:update_tags" in routes and all(route.startswith("view") for route in routes), routes


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def test_run_without_startup_sweep_waits_a_full_interval_first():
    # The weaker ancestor of this test only counted sweeps; this one proves
    # run_on_startup=False actually *waits*: the first sweep may not happen
    # before one whole (unjittered) interval has passed.
    indexer, _, _ = make()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    swept_at: list[float] = []
    real_sweep = indexer.sweep

    async def timed_sweep():
        swept_at.append(loop.time())
        stop.set()
        return await real_sweep()

    indexer.sweep = timed_sweep  # type: ignore[method-assign]
    started = loop.time()
    await asyncio.wait_for(indexer.run(interval_seconds=0.2, run_on_startup=False, jitter=0.0, stop=stop), timeout=5)

    assert len(swept_at) == 1
    assert swept_at[0] - started >= 0.19, "the first sweep ran before the interval elapsed"


async def test_run_sweeps_on_startup_then_on_the_interval_until_stopped():
    indexer, _, _ = make()
    sweeps: list[SweepSummary] = []
    stop = asyncio.Event()

    real_sweep = indexer.sweep

    async def counting_sweep():
        summary = await real_sweep()
        sweeps.append(summary)
        if len(sweeps) == 3:
            stop.set()
        return summary

    indexer.sweep = counting_sweep  # type: ignore[method-assign]
    await asyncio.wait_for(indexer.run(interval_seconds=0.01, jitter=0.0, stop=stop), timeout=5)

    assert len(sweeps) == 3
    assert sweeps[0].indexed == 2 and sweeps[1].skipped == 2


async def test_run_can_skip_the_startup_sweep_and_survives_a_failing_sweep():
    indexer, _, _ = make()
    calls: list[str] = []
    stop = asyncio.Event()

    async def failing_sweep():
        calls.append("sweep")
        if len(calls) == 2:
            stop.set()
        raise RuntimeError("store outage")

    indexer.sweep = failing_sweep  # type: ignore[method-assign]
    task = asyncio.create_task(indexer.run(interval_seconds=0.01, run_on_startup=False, jitter=0.0, stop=stop))
    await asyncio.wait_for(task, timeout=5)

    assert calls == ["sweep", "sweep"], "no startup sweep; the loop kept going after the first failure"


async def test_run_rejects_a_non_positive_interval():
    indexer, _, _ = make()
    with pytest.raises(ValueError):
        await indexer.run(interval_seconds=0)


async def test_run_is_cancellable():
    indexer, _, _ = make()
    task = asyncio.create_task(indexer.run(interval_seconds=60, run_on_startup=False))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# App wiring (config builder + lifespan)
# ---------------------------------------------------------------------------


def cogs_block(*sources: dict, enabled: bool = True, **index) -> dict:
    """A ``cogs:`` config block (#87) as ``Config.parse`` takes it."""

    return {"registry_sources": list(sources), "index": {"enabled": enabled, **index}}


STATIC_SOURCE = {"id": "s", "kind": "static", "url": URL, "repositories": ["cogs/x"]}


def test_build_cog_indexing_is_none_without_sources_or_without_a_store():
    from collab_hub_api.config import build_cog_indexing

    assert build_cog_indexing(Config.parse(), InMemoryCogCatalogStore()) is None
    # Sources but no database, sweeping off: nothing an indexer could write.
    disabled = Config.parse({"cogs": cogs_block(STATIC_SOURCE, enabled=False)})
    assert build_cog_indexing(disabled, UnavailableCogCatalogStore()) is None


def test_build_cog_indexing_builds_a_sweepless_indexer_when_indexing_is_off():
    from collab_hub_api.config import build_cog_catalog_store, build_cog_indexing, build_postgres_pools

    # What every API replica is given (the chart renders enabled=false on
    # them, issue #148): the sources and the targeted entry points, no loop.
    # The one-connection pool the sweeping process refuses is fine here.
    config = Config.parse(
        {
            "frames": {"postgres": {"url": "postgresql://shared/db", "pool": {"max_size": 1, "min_size": 1}}},
            "cogs": cogs_block(STATIC_SOURCE, enabled=False),
        }
    )
    store = build_cog_catalog_store(config, build_postgres_pools(config))

    indexing = build_cog_indexing(config, store)

    assert indexing is not None and indexing.sweeps is False
    assert [s.id for s in indexing.indexer.sources] == ["s"]
    indexing.indexer.close()


def test_build_cog_indexing_builds_sources_and_reads_the_loop_parameters():
    from collab_hub_api.config import build_cog_indexing

    config = Config.parse({"cogs": cogs_block(STATIC_SOURCE, interval_seconds=42, run_on_startup=False)})

    indexing = build_cog_indexing(config, InMemoryCogCatalogStore())

    assert indexing is not None and indexing.sweeps is True
    assert indexing.interval_seconds == 42.0 and indexing.run_on_startup is False
    assert [s.id for s in indexing.indexer.sources] == ["s"]


def test_build_cog_indexing_refuses_a_one_connection_pool():
    from collab_hub_api.config import build_cog_catalog_store, build_cog_indexing, build_postgres_pools

    # The sweep occupies one pooled connection for its whole duration (the
    # lock is held on it and the sweep's reads and writes ride it, #128);
    # with max_size=1 that leaves nothing for API reads or webhook writes,
    # which would wait on the sweep and time out, silently, at runtime.
    config = Config.parse(
        {
            "frames": {"postgres": {"url": "postgresql://shared/db", "pool": {"max_size": 1, "min_size": 1}}},
            "cogs": cogs_block(STATIC_SOURCE),
        }
    )
    pools = build_postgres_pools(config)
    store = build_cog_catalog_store(config, pools)
    with pytest.raises(RuntimeError, match="max_size >= 2"):
        build_cog_indexing(config, store)


def test_build_cog_indexing_refuses_the_unavailable_store():
    from collab_hub_api.config import build_cog_indexing

    config = Config.parse({"cogs": cogs_block(STATIC_SOURCE)})
    with pytest.raises(RuntimeError, match="cogs.index.enabled requires the Cog catalog store"):
        build_cog_indexing(config, UnavailableCogCatalogStore())


async def test_app_exposes_the_store_and_no_indexer_without_sources(config, monkeypatch):
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(config)
    async with app.router.lifespan_context(app):
        # No shared Postgres and no sources in the test config: the store
        # refuses (503 at the API), and there is nothing to build an indexer on.
        assert isinstance(app.state.cog_catalog_store, UnavailableCogCatalogStore)
        assert app.state.cog_indexer is None and app.state.cog_registry_sources == []


def _app_config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
            "cogs": cogs_block(STATIC_SOURCE, interval_seconds=3600, run_on_startup=True),
        }
    )


async def test_app_shutdown_does_not_wait_for_a_stuck_store_call(tmp_path, monkeypatch, caplog):
    # Issue #148: a store call that never returns (a dead transport) is not
    # drained. The cancelled sweep unwinds at once, the lifespan exits at
    # once, and the exit chained onto the stuck acquisition still releases
    # the lock on that thread when -- if -- the acquisition returns. In the
    # pod, the process exits first and the session takes the lock with it.
    from collab_hub_api import config as config_module
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    store = _EventedStore()
    store.release_enter.clear()  # the sweep's lock acquisition never returns
    monkeypatch.setattr(config_module, "build_cog_catalog_store", lambda *_: store)
    monkeypatch.setattr("collab_hub_api.core.build_cog_catalog_store", lambda *_: store)

    app = make_app(_app_config(tmp_path))
    loop = asyncio.get_running_loop()
    async with app.router.lifespan_context(app):
        await asyncio.to_thread(store.enter_started.wait, 10)
        task = {t.get_name(): t for t in asyncio.all_tasks()}["cog-index"]
        started = loop.time()
        with caplog.at_level(logging.ERROR, logger="frames_server.core"):
            pass
    elapsed = loop.time() - started
    # Well under the 5 s lifespan bound: the worker is blocked until the test
    # releases it, so any exit before that bound proves nothing waited on it.
    assert elapsed < 3, f"lifespan exit took {elapsed:.1f}s: the cancelled sweep waited for its worker"
    assert task.cancelled(), "the task unwound without waiting for the blocked acquisition"
    assert "cog_indexer_shutdown_abandoned" not in [r.message for r in caplog.records]
    assert store.events == [], "nothing was released under -- or before -- the still-running acquisition"
    # The task is done, so the lifespan closed the executors at once...
    indexer = app.state.cog_indexer
    for executor in (indexer._executor, indexer._sweep_thread):
        with pytest.raises(RuntimeError, match="shutdown"):
            executor.submit(lambda: None)
    # ...and the release, already queued on the sweep thread behind the
    # acquisition, still runs the moment __enter__ returns.
    store.release_enter.set()
    await _until(lambda: store.events == ["entered", "unlocked"])
    assert store.exit_thread is store.enter_thread
    with store.sweep_lock() as held:
        assert held is not None


async def test_app_shutdown_is_bounded_when_the_indexer_task_does_not_unwind(tmp_path, monkeypatch, caplog):
    # The lifespan's wait is bounded regardless of what the task does with
    # its cancellation, so a database that stopped answering -- or a future
    # sweep that defers cancellation -- cannot hang app shutdown: past the
    # deadline the task is left pending, loudly, and dies with the process.
    from collab_hub_api import config as config_module
    from collab_hub_api import core
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setattr(core, "INDEXER_SHUTDOWN_TIMEOUT_SECONDS", 0.3)
    let_go = asyncio.Event()

    async def run_that_swallows_cancellation(self, **_kwargs):
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if let_go.is_set():
                    raise RuntimeError("late failure: postgresql://user:secret@db.internal/collab") from None

    monkeypatch.setattr(CogIndexer, "run", run_that_swallows_cancellation)
    store = InMemoryCogCatalogStore()
    monkeypatch.setattr(config_module, "build_cog_catalog_store", lambda *_: store)
    monkeypatch.setattr("collab_hub_api.core.build_cog_catalog_store", lambda *_: store)

    app = make_app(_app_config(tmp_path))
    loop = asyncio.get_running_loop()
    async with app.router.lifespan_context(app):
        task = {t.get_name(): t for t in asyncio.all_tasks()}["cog-index"]
        await asyncio.sleep(0)  # let the task reach its first await; a task cancelled before it starts just ends
        started = loop.time()
        with caplog.at_level(logging.ERROR, logger="frames_server.core"):
            pass
    elapsed = loop.time() - started
    assert elapsed < 3, f"lifespan exit took {elapsed:.1f}s: shutdown is not bounded"
    assert not task.done(), "the task that would not unwind is left pending, not waited out"
    assert "cog_indexer_shutdown_abandoned" in [r.message for r in caplog.records]

    # The executors are closed once the task does finish, whenever that is
    # -- and a task that then fails is observed, by class name, rather than
    # left for asyncio to report at collection (codex round-2 finding).
    executor = app.state.cog_indexer._executor
    executor.submit(lambda: None).result(timeout=5)
    let_go.set()
    task.cancel()
    with caplog.at_level(logging.ERROR, logger="frames_server.core"):
        with pytest.raises(RuntimeError, match="late failure"):
            await asyncio.wait_for(task, timeout=5)
        await asyncio.sleep(0.05)
    failed = [r for r in caplog.records if r.message == "cog_indexer_task_failed"]
    assert failed and failed[0].error == "RuntimeError"
    for closed in (executor, app.state.cog_indexer._sweep_thread):
        with pytest.raises(RuntimeError, match="shutdown"):
            closed.submit(lambda: None)


async def _until(condition, *, timeout: float = 5.0) -> None:
    """Poll ``condition`` (a plain callable) until true; the worker threads under test are not awaitable."""

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


async def test_app_runs_the_indexer_in_its_lifespan_and_closes_sources_on_shutdown(tmp_path, monkeypatch):
    from collab_hub_api import config as config_module
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
            "cogs": cogs_block(STATIC_SOURCE, interval_seconds=3600, run_on_startup=False),
        }
    )
    # No Postgres in this config, so stand the store in: what is under test
    # is the lifespan's ownership of the task and the sources.
    store = InMemoryCogCatalogStore()
    monkeypatch.setattr(config_module, "build_cog_catalog_store", lambda *_: store)
    monkeypatch.setattr("collab_hub_api.core.build_cog_catalog_store", lambda *_: store)

    app = make_app(config)
    async with app.router.lifespan_context(app):
        indexer = app.state.cog_indexer
        assert isinstance(indexer, CogIndexer) and app.state.cog_catalog_store is store
        (registry_source,) = app.state.cog_registry_sources
        tasks = {t.get_name(): t for t in asyncio.all_tasks()}
        assert "cog-index" in tasks and not tasks["cog-index"].done()
        assert not registry_source._closed
    assert tasks["cog-index"].cancelled()
    assert registry_source._closed, "the lifespan closes the registry clients it built"


async def test_an_api_replica_holds_the_indexer_without_sweeping(tmp_path, monkeypatch):
    # The chart renders cogs.index.enabled=false on every API replica (issue
    # #148), and those are the only pods a Service reaches: the indexer and
    # its targeted entry points have to exist there, with no sweep loop.
    from collab_hub_api import config as config_module
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
            "cogs": cogs_block(STATIC_SOURCE, enabled=False),
        }
    )
    store = InMemoryCogCatalogStore()
    monkeypatch.setattr(config_module, "build_cog_catalog_store", lambda *_: store)
    monkeypatch.setattr("collab_hub_api.core.build_cog_catalog_store", lambda *_: store)

    app = make_app(config)
    async with app.router.lifespan_context(app):
        indexer = app.state.cog_indexer
        assert isinstance(indexer, CogIndexer)
        (registry_source,) = app.state.cog_registry_sources
        assert "cog-index" not in {t.get_name() for t in asyncio.all_tasks()}, "an API replica must never sweep"
        # A targeted entry point works here: no sweep lock, the shared pool.
        assert await indexer.mark_removed("s", "cogs/x", "sha256:" + "0" * 64) is False
        with pytest.raises(KeyError):
            await indexer.mark_removed("unknown", "cogs/x", "sha256:" + "0" * 64)
        assert not registry_source._closed
    assert indexer.last_summary is None, "nothing swept"
    assert registry_source._closed, "the lifespan closes the registry clients it built"
    for executor in (indexer._executor, indexer._sweep_thread):
        with pytest.raises(RuntimeError, match="shutdown"):
            executor.submit(lambda: None)


# ---------------------------------------------------------------------------
# Cancellation: a cancelled sweep releases its lock or dies with its session
# (issue #148 -- the drain, its deadline and the thread hand-off are gone)
# ---------------------------------------------------------------------------


class _EventedStore(InMemoryCogCatalogStore):
    """An in-memory store whose lock and writes can be held open from the test.

    ``events`` records the operations that matter. Worker threads cannot be
    interrupted and the sweep no longer waits for them, so a cancellation
    landing inside a blocked operation returns at once; what the tests assert
    is that the lock is still released exactly once -- by the exit chained
    onto a blocked acquisition, or by the release submitted past a blocked
    write -- and never twice.
    """

    def __init__(self):
        super().__init__()
        self.events: list[str] = []
        self.enter_started = threading.Event()
        self.release_enter = threading.Event()
        self.write_started = threading.Event()
        self.release_write = threading.Event()
        self.exit_started = threading.Event()
        self.release_exit = threading.Event()
        self.fail_write: BaseException | None = None
        self.enter_thread: threading.Thread | None = None
        self.exit_thread: threading.Thread | None = None
        self.entered = threading.Event()
        # Default: nothing blocks unless a test arms it.
        self.release_enter.set()
        self.release_write.set()
        self.release_exit.set()

    @contextmanager
    def sweep_lock(self):
        self.enter_started.set()
        assert self.release_enter.wait(timeout=10)
        self.enter_thread = threading.current_thread()
        self.events.append("entered")
        self.entered.set()
        try:
            with super().sweep_lock() as held:
                yield held
        finally:
            self.exit_started.set()
            assert self.release_exit.wait(timeout=10)
            self.exit_thread = threading.current_thread()
            self.events.append("unlocked")

    def upsert(self, artifact):
        self.write_started.set()
        assert self.release_write.wait(timeout=10)
        if self.fail_write is not None:
            raise self.fail_write
        super().upsert(artifact)
        self.events.append("write_done")


def evented_indexer(**kwargs):
    base, _, client = make(**kwargs)
    store = _EventedStore()
    return CogIndexer(store, base.sources, **kwargs), store, client


async def test_cancellation_during_lock_acquisition_still_releases():
    # Nothing waits for the blocked acquisition, but whatever it enters gets
    # its matching exit: queued behind it on the sweep thread, run the moment
    # __enter__ returns -- never before it, never on the event loop.
    indexer, store, _ = evented_indexer()
    store.release_enter.clear()

    task = asyncio.create_task(indexer.sweep())
    await asyncio.to_thread(store.enter_started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert store.events == [], "the cancelled sweep neither waited for the acquisition nor exited under it"

    store.release_enter.set()
    await _until(lambda: store.events == ["entered", "unlocked"])
    assert store.exit_thread is not threading.main_thread() and store.exit_thread is store.enter_thread, (
        "the exit ran on the sweep thread, behind the acquisition"
    )
    with store.sweep_lock() as held:
        assert held is not None


async def test_cancellation_during_a_write_queues_the_release_behind_it_without_waiting():
    # The post-#148 contract: a cancelled sweep releases its lock (or dies
    # with its session) and does not wait for the write it abandoned. The
    # release is queued behind that write on the sweep thread, so it lands
    # the moment the write returns -- and never before, whatever store the
    # write is on: the ordering is the thread's, not psycopg's (codex round-1
    # finding: a call that had started but not yet reached the connection
    # could be overtaken by a release on another thread).
    indexer, store, _ = evented_indexer()
    store.release_write.clear()
    cancelled_before = counter(COG_INDEX_SWEEPS, result="cancelled")
    failed_before = counter(COG_INDEX_SWEEPS, result="failed")

    task = asyncio.create_task(indexer.sweep())
    await asyncio.to_thread(store.write_started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # A shutdown mid-sweep is its own result, not a failed sweep.
    assert counter(COG_INDEX_SWEEPS, result="cancelled") == cancelled_before + 1
    assert counter(COG_INDEX_SWEEPS, result="failed") == failed_before

    await asyncio.sleep(0.1)
    assert store.events == ["entered"], "no release while the abandoned write is still running"
    with InMemoryCogCatalogStore.sweep_lock(store) as held:  # the base class, so the probe is not an event
        assert held is None, "the lock is still held: the release is queued behind the write"

    store.release_write.set()
    await _until(lambda: "unlocked" in store.events)
    assert store.events == ["entered", "write_done", "unlocked"]
    with store.sweep_lock() as held:
        assert held is not None


async def test_an_acquisition_that_completes_before_the_cancel_is_handled_exits_on_the_sweep_thread():
    # Codex round-1 finding: with the exit chained as a done-callback, an
    # acquisition that had already completed by the time the cancellation
    # handler ran got its exit *inline*, on the event loop. The release is
    # now queued on the sweep thread whatever the timing. Deterministic: the
    # loop is held (no await) between task.cancel() and the acquisition
    # finishing, so the handler cannot run until the future is done.
    indexer, store, _ = evented_indexer()
    store.release_enter.clear()
    submitted: list = []
    submit = indexer._sweep_thread.submit

    def recording_submit(fn, /, *args, **kwargs):
        future = submit(fn, *args, **kwargs)
        submitted.append(future)
        return future

    indexer._sweep_thread.submit = recording_submit  # type: ignore[method-assign]

    task = asyncio.create_task(indexer.sweep())
    await asyncio.to_thread(store.enter_started.wait, 10)
    task.cancel()
    store.release_enter.set()
    # Blocking on purpose: the loop does not run, so the cancellation is not
    # handled until the acquisition's own future has settled (codex round-3
    # finding: an event set inside __enter__ plus a sleep was not that).
    assert submitted[0].result(timeout=10) is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    await _until(lambda: store.events == ["entered", "unlocked"])
    assert store.exit_thread is not threading.main_thread(), "the exit must not run on the event loop"
    assert store.exit_thread is store.enter_thread, "it ran on the sweep thread, behind the acquisition"
    with store.sweep_lock() as held:
        assert held is not None


async def test_a_release_the_sweep_is_waiting_for_survives_a_cancel_while_it_is_still_queued():
    # Codex round-1 finding: the normal path awaited its release through
    # wrap_future, which cancels a future that has not started -- so a
    # cancellation arriving while the release sat behind another call on
    # the sweep thread dropped the release outright. This pins HEAD's
    # mechanism end to end (the bridge itself is pinned by
    # test_awaiting_without_cancelling_leaves_the_worker_alone below):
    # a blocker is put on the sweep thread just before the release is
    # queued, so the sweep is waiting on a release that is queued, not
    # running, when the cancel lands.
    indexer, store, _ = evented_indexer()
    blocker = threading.Event()
    blocker_running = threading.Event()
    release_queued = threading.Event()
    queue_release = indexer._queue_release

    def block() -> None:
        blocker_running.set()
        blocker.wait(10)

    def queue_behind_a_blocker(lock, entering):
        indexer._sweep_thread.submit(block)
        release = queue_release(lock, entering)
        release_queued.set()
        return release

    indexer._queue_release = queue_behind_a_blocker  # type: ignore[method-assign]
    task = asyncio.create_task(indexer.sweep())
    # The sweep has written both Cogs and is now waiting on its release,
    # which sits in the queue behind the running blocker.
    await _until(lambda: store.events == ["entered", "write_done", "write_done"])
    # The one sweep thread is inside the blocker, so the release submitted
    # after it can only be queued.
    await _until(lambda: blocker_running.is_set() and release_queued.is_set())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert store.events == ["entered", "write_done", "write_done"], "the release is still queued behind the blocker"

    blocker.set()
    await _until(lambda: store.events == ["entered", "write_done", "write_done", "unlocked"])
    with store.sweep_lock() as held:
        assert held is not None, "the queued release ran once the thread freed; nothing dropped it"


async def test_awaiting_without_cancelling_leaves_the_worker_alone():
    # The bridge the normal path awaits its release through. wrap_future
    # would cancel a queued worker when the awaiter is cancelled; this must
    # not -- the worker runs once the thread frees, and its result and
    # exception still reach an awaiter that is there to receive them.
    from collab_hub_api.cogs.indexer import _await_without_cancelling

    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = threading.Event()
        ran = threading.Event()
        pool.submit(blocker.wait, 10)
        queued = pool.submit(ran.set)

        waiter = asyncio.create_task(_await_without_cancelling(queued))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not queued.cancelled() and not queued.done(), "the awaiter's cancellation did not reach the worker"
        blocker.set()
        assert await asyncio.to_thread(ran.wait, 5), "the queued worker still ran"

        # Results and exceptions travel; a worker the executor cancelled is an error, not a hang.
        assert await _await_without_cancelling(pool.submit(lambda: 42)) == 42
        with pytest.raises(ValueError, match="boom"):
            await _await_without_cancelling(pool.submit(_raise, ValueError("boom")))
    stopped = ThreadPoolExecutor(max_workers=1)
    hold = threading.Event()
    stopped.submit(hold.wait, 10)
    never = stopped.submit(lambda: None)
    stopped.shutdown(wait=False, cancel_futures=True)
    hold.set()
    with pytest.raises(RuntimeError, match="cancelled before it ran"):
        await _await_without_cancelling(never)


def _raise(exc: BaseException) -> None:
    raise exc


async def test_a_cancelled_sweeps_queued_release_survives_a_second_cancel_and_close():
    # Codex round-1 finding: a queued release awaited through wrap_future was
    # cancelled away by a second cancel, and executor shutdown with
    # cancel_futures dropped it too. Now nothing the sweep does after
    # queueing the release can un-queue it: cancel again, close the indexer,
    # and the release still runs once the abandoned write returns.
    indexer, store, _ = evented_indexer()
    store.release_write.clear()

    task = asyncio.create_task(indexer.sweep())
    await asyncio.to_thread(store.write_started.wait, 10)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    indexer.close()  # queued work on the sweep thread is not cancelled

    store.release_write.set()
    await _until(lambda: "unlocked" in store.events)
    assert store.events == ["entered", "write_done", "unlocked"]


async def test_cancellation_during_unlock_completes_the_unlock_exactly_once(caplog):
    indexer, store, _ = evented_indexer()
    store.release_exit.clear()

    task = asyncio.create_task(CogIndexer(store, []).sweep())
    await asyncio.to_thread(store.exit_started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert store.events == ["entered"], "the cancelled release did not wait for the blocked unlock"

    with caplog.at_level(logging.ERROR, logger="frames_server.cogs.indexer"):
        store.release_exit.set()
        await _until(lambda: store.events == ["entered", "unlocked"])
        await asyncio.sleep(0.05)
    assert "cog_index_lock_release_failed" not in [r.message for r in caplog.records], "no second release"
    with store.sweep_lock() as held:
        assert held is not None


def test_retry_rotation_is_keyed_by_identity_not_position():
    # Round-4 codex repro: the rotation was an integer cursor, so a candidate
    # list that changed shape between sweeps moved what that position pointed
    # at -- with new failures arriving ahead of the old ones, the originals
    # were stepped over every sweep and never retried. The order is a list of
    # (repository, digest) identities now.
    indexer, _, _ = make()

    def candidate(digest: str):
        return ("cogs/r", ArtifactRef(digest=digest, tags=("v1",)), None)

    first = indexer._retry_candidates(SOURCE_ID, [candidate("d1"), candidate("d2"), candidate("d3")])
    assert [ref.digest for _, ref, _ in first] == ["d1", "d2", "d3"], "enumeration order, the first time"

    indexer._note_retry_attempt(SOURCE_ID, ("cogs/r", "d1"))
    # Next sweep: d1 recovered and is gone, d0 is newly failed and sorts FIRST
    # in enumeration order -- exactly the reshuffle the cursor could not take.
    second = indexer._retry_candidates(SOURCE_ID, [candidate("d0"), candidate("d2"), candidate("d3")])
    assert [ref.digest for _, ref, _ in second] == ["d2", "d3", "d0"], (
        "remembered candidates keep their order and newcomers go behind them"
    )
    assert ("cogs/r", "d1") not in indexer._retry_order[SOURCE_ID], "a recovered candidate is forgotten"


async def test_an_interrupted_sweep_does_not_rotate_past_an_unattempted_retry():
    # The rotation advances as an attempt BEGINS, so a sweep that dies before
    # reaching a candidate leaves that candidate at the front for the next one.
    poison = dict(COMPLETE)
    poison[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\x00tail"
    artifacts = {"cogs/p": [entry("1", ["v1"], poison)], "cogs/q": [entry("2", ["v1"], poison)]}
    indexer, _, _ = make(artifacts, max_new_fetches_per_sweep=2)
    await indexer.sweep()  # both become failed rows
    await indexer.sweep()  # and are established in the rotation
    before = list(indexer._retry_order[SOURCE_ID])
    assert len(before) == 2

    async def explode(*args, **kwargs):
        raise RuntimeError("the sweep dies during the first retry")

    indexer._reconcile = explode
    with pytest.raises(RuntimeError):
        await indexer.sweep()

    # The first candidate's attempt began (it is rotated to the back); the
    # second was never reached and is still at the front.
    assert indexer._retry_order[SOURCE_ID] == [before[1], before[0]]


# ---------------------------------------------------------------------------
# The fetch budget defers work without starving it or breaking removal
# ---------------------------------------------------------------------------


async def test_fetch_budget_makes_progress_across_sweeps_and_removal_stays_on():
    artifacts = {
        "cogs/a": [entry("1", ["v"], COMPLETE)],
        "cogs/b": [entry("2", ["v"], CONTEXT)],
        "cogs/c": [entry("3", ["v"], PROG)],
    }
    indexer, store, _ = make(artifacts, max_new_fetches_per_sweep=1)
    # A stale row for this source: its digest is gone from the registry, and
    # deferring fetches must NOT defer noticing that -- enumeration was
    # complete, so removal is still safe and still runs.
    stale = CogArtifact(
        source_id=SOURCE_ID,
        host=HOST,
        repository="cogs/a",
        digest="sha256:" + "9" * 64,
        status=STATUS_INDEXED,
        tags=("old",),
        pushed_at=PUSHED,
    )
    store.upsert(stale)

    first = await indexer.sweep()
    assert (first.indexed, first.deferred, first.removed, first.sources_failed) == (1, 2, 1, 0)
    assert store.get(stale.digest).removed_at is not None

    second = await indexer.sweep()
    assert (second.indexed, second.deferred, second.skipped) == (1, 1, 1), "the second sweep reached the next artifact"

    third = await indexer.sweep()
    assert (third.indexed, third.deferred, third.skipped) == (1, 0, 2)
    assert (await indexer.sweep()).skipped == 3, "steady state: everything known, nothing deferred"


# ---------------------------------------------------------------------------
# A truncated enumeration must never remove (codex-gate HIGH finding)
# ---------------------------------------------------------------------------


async def test_over_bound_tag_list_fails_the_source_instead_of_removing():
    indexer, store, client = make()
    await indexer.sweep()

    # The registry sprouts more tags than the static adapter's bound in one
    # repository. Before the fix the adapter silently kept a sorted prefix,
    # the sweep treated it as complete, and artifacts displaced past the
    # window were marked removed.
    manifest = client.manifests[("cogs/notes", "v1")]
    extra = [f"t{i:03d}" for i in range(MAX_TAGS_PER_REPOSITORY)]
    client.tags["cogs/notes"] = ["v1", *extra]
    for tag in extra:
        client.manifests[("cogs/notes", tag)] = manifest

    summary = await indexer.sweep()

    assert summary.sources_failed == 1 and summary.removed == 0
    assert "RegistrySourceProtocolError" in summary.errors[0]
    assert store.get("sha256:" + "b" * 64).present, "nothing was removed off a truncated view"
    assert store.get("sha256:" + "a" * 64).present


# ---------------------------------------------------------------------------
# Failure isolation is broader than the OCI hierarchy
# ---------------------------------------------------------------------------


async def test_unexpected_reader_exception_is_one_failed_row_with_class_only(monkeypatch):
    # A genuine reader-side failure (the previous test of this name deleted a
    # blob, which is a *fetch* failure): the read/convert boundary must catch
    # anything, record the class -- never the message, whose content is not
    # known to be safe -- and keep sweeping.
    indexer, store, _ = make()

    def boom(files, *, bundle_paths=None):
        raise RuntimeError("message that could quote anything, even a token")

    monkeypatch.setattr("collab_hub_api.cogs.indexer.read_cog_bundle", boom)

    summary = await indexer.sweep()

    assert summary.failed == 2 and summary.indexed == 0
    row = store.get("sha256:" + "a" * 64)
    assert row.status == STATUS_FAILED
    assert row.read_errors == ("read: RuntimeError",)


async def test_unstorable_card_is_one_failed_row_and_the_sweep_continues():
    # A COG.md whose body carries NUL reads fine but cannot live in jsonb;
    # it must become a failed row with a reason, not a sweep-aborting (and
    # every-sweep-repeating) database error.
    poison = dict(COMPLETE)
    poison[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\x00tail"
    indexer, store, _ = make(
        {
            "cogs/poison": [entry("e", ["v1"], poison)],
            "cogs/notes": [entry("b", ["v1"], CONTEXT)],
        }
    )

    summary = await indexer.sweep()

    assert (summary.failed, summary.indexed) == (1, 1)
    row = store.get("sha256:" + "e" * 64)
    assert row.status == STATUS_FAILED and row.card is None
    assert row.read_errors[0] == "card: contains NUL (\\u0000), which jsonb cannot store"
    assert store.get("sha256:" + "b" * 64).status == STATUS_INDEXED
    # Steady state: the poison row is failed, so it is retried -- and fails
    # the same bounded way -- rather than silently forgotten.
    assert (await indexer.sweep()).failed == 1


async def test_literal_backslash_u0000_text_indexes_fine():
    # Round-2 codex finding: the NUL check once searched the serialized JSON
    # for the substring \\u0000, which also matches documentation that merely
    # *discusses* NUL. Only an actual NUL character makes a card unstorable.
    literal = dict(COMPLETE)
    literal[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\nThe escape sequence \\u0000 denotes NUL.\n"
    indexer, store, _ = make({"cogs/doc": [entry("d", ["v1"], literal)]})

    summary = await indexer.sweep()

    assert (summary.indexed, summary.failed) == (1, 0)
    row = store.get("sha256:" + "d" * 64)
    assert row.status == STATUS_INDEXED
    assert "\\u0000" in row.card["body"]


async def test_store_data_error_falls_back_to_a_failed_row():
    # Second line of defense behind the pre-validation: whatever
    # representability rule the store enforces that the validation did not
    # anticipate becomes a failed row, while outages still propagate.
    class PickyStore(InMemoryCogCatalogStore):
        def upsert(self, artifact):
            if artifact.card is not None:
                raise CogCatalogDataError("UntranslatableCharacter")
            super().upsert(artifact)

    base, _, _ = make()
    store = PickyStore()
    summary = await CogIndexer(store, base.sources).sweep()

    assert summary.failed == 2 and summary.indexed == 0
    row = store.get("sha256:" + "a" * 64)
    assert row.status == STATUS_FAILED and row.card is None
    assert row.read_errors == ("store: CogCatalogDataError",)


# ---------------------------------------------------------------------------
# The lockfile is excluded by media type even on direct fetches
# ---------------------------------------------------------------------------


def lock_disguised_as(title: str) -> Descriptor:
    data = b"lock: contents\n"
    return Descriptor(
        media_type=MEDIA_TYPE_PIXI_LOCK,
        digest=blob_digest(data),
        size=len(data),
        annotations={TITLE_ANNOTATION: title},
    )


async def test_lockfile_disguised_as_cog_md_is_not_fetched():
    disguised = lock_disguised_as(COG_ENTRY_FILE)
    indexer, store, client = make({"images/sneaky": [entry("e", ["v1"], {}, extra_layers=(disguised,))]})
    client.blobs[disguised.digest] = b"lock: contents\n"

    summary = await indexer.sweep()

    assert summary.non_cog == 1
    assert blob_calls(client) == [], "the disguised lockfile was never fetched"
    assert store.get("sha256:" + "e" * 64).status == STATUS_NON_COG


async def test_lockfile_disguised_as_pixi_toml_is_not_fetched():
    disguised = lock_disguised_as("pixi.toml")
    indexer, store, client = make({"images/sneaky": [entry("e", ["v1"], {}, extra_layers=(disguised,))]})
    client.blobs[disguised.digest] = b"lock: contents\n"

    summary = await indexer.sweep()

    assert summary.non_cog == 1
    assert blob_calls(client) == []
    assert store.get("sha256:" + "e" * 64).status == STATUS_NON_COG


# ---------------------------------------------------------------------------
# Live-Postgres: a poison card and a valid card in the same sweep
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres indexer tests",
)


@live_postgres
async def test_live_poison_card_fails_one_row_and_the_valid_one_indexes():
    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.cogs.catalog import PostgresCogCatalogStore
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    poison = dict(COMPLETE)
    poison[COG_ENTRY_FILE] = COMPLETE[COG_ENTRY_FILE] + b"\x00tail"
    base, _, _ = make(
        {
            "cogs/poison": [entry("e", ["v1"], poison)],
            "cogs/notes": [entry("b", ["v1"], CONTEXT)],
        }
    )

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=10, timeout_seconds=10.0)

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        store = PostgresCogCatalogStore(database)
        indexer = CogIndexer(store, base.sources)

        summary = await indexer.sweep()

        assert (summary.failed, summary.indexed) == (1, 1)
        row = store.get("sha256:" + "e" * 64)
        assert row.status == STATUS_FAILED and row.card is None
        assert row.read_errors[0].startswith("card: contains NUL")
        assert store.get("sha256:" + "b" * 64).status == STATUS_INDEXED
        # And a second sweep is not poisoned either.
        assert (await indexer.sweep()).failed == 1
        drop_all()
    finally:
        database.close()
