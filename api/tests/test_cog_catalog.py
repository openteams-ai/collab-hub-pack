"""The Cog catalog store (issue #84): in-memory contract coverage plus live-Postgres coverage.

Two layers, as for the other ``collab_`` tables (see ``test_collab_schema.py``):

- **Always-on** coverage of the store contract against ``InMemoryCogCatalogStore``,
  including the ``jsonb @>`` containment semantics the in-memory filter mirrors.
- **Opt-in** live coverage (``COLLAB_HUB_TEST_POSTGRES_URL``) where the same
  contract is proven against the real table: upsert/update/removal, tag change
  without a card write, containment filters that genuinely reach the GIN
  index (checked with EXPLAIN), and the session-level sweep lock under real
  concurrent connections.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from collab_hub_api.cogs.catalog import (
    COG_INDEX_LOCK_KEY,
    MAX_LIST_LIMIT,
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    SWEEP_STATEMENT_TIMEOUT_SECONDS,
    CatalogFilter,
    CogArtifact,
    CogCatalogDataError,
    CogCatalogUnavailableError,
    InMemoryCogCatalogStore,
    KnownArtifact,
    PostgresCogCatalogStore,
    UnavailableCogCatalogStore,
    card_search_fields,
    contains_nul,
    json_contains,
    like_pattern,
    matches_query,
)
from collab_hub_api.frames.collab_schema import COLLAB_SCHEMA_LOCK_KEY, run_collab_schema_migrations
from collab_hub_api.frames.db import FRAMES_SERVER_SCHEMA_LOCK_KEY

HOST = "registry.example"
SOURCE = "main"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def card(
    cog_id: str = "example/cog-a",
    *,
    version: str = "1.0.0",
    kind: str = "complete",
    publisher: str | None = "Example Organization",
    provides: list | None = None,
    requires: list[str] = (),
    accepts: list[str] = (),
    produces: list[str] = (),
) -> dict:
    """A card in the reader's shape, with the parts the filters read."""

    return {
        "card": 1,
        "id": cog_id,
        "name": cog_id.rsplit("/", 1)[-1],
        "version": version,
        "kind": kind,
        "publisher": publisher,
        "manifest_schema": "openteams/cog-manifest [0.1]",
        "provides": provides if provides is not None else [],
        "requires": [{"capability": cap, "locality": "any", "satisfiers": []} for cap in requires],
        "io": {"accepts": list(accepts), "produces": list(produces)},
        "errors": [],
    }


def artifact(
    seed: str,
    *,
    repository: str = "cogs/cog-a",
    source_id: str = SOURCE,
    status: str = STATUS_INDEXED,
    tags: tuple[str, ...] = ("v1",),
    pushed_at: datetime | None = T0,
    document: dict | None = None,
    read_errors: tuple[str, ...] = (),
) -> CogArtifact:
    document = document if document is not None else (card() if status == STATUS_INDEXED else None)
    fields = card_search_fields(document) if document else {}
    return CogArtifact(
        source_id=source_id,
        host=HOST,
        repository=repository,
        digest=digest(seed),
        status=status,
        tags=tags,
        pushed_at=pushed_at,
        manifest_media_type="application/vnd.oci.image.manifest.v1+json",
        card=document,
        read_errors=read_errors,
        **fields,
    )


# ---------------------------------------------------------------------------
# Contract, against the in-memory store
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryCogCatalogStore:
    return InMemoryCogCatalogStore()


def test_lock_key_is_distinct_from_the_other_advisory_keys():
    assert len({COG_INDEX_LOCK_KEY, COLLAB_SCHEMA_LOCK_KEY, FRAMES_SERVER_SCHEMA_LOCK_KEY}) == 3
    assert COG_INDEX_LOCK_KEY == int.from_bytes(b"cogidx_1", "big")


def test_upsert_then_get_by_digest_returns_the_row_with_its_reference(store):
    row = artifact("a")
    store.upsert(row)

    got = store.get(row.digest)
    assert got is not None
    assert got.card == card()
    assert got.cog_id == "example/cog-a" and got.name == "cog-a" and got.version == "1.0.0"
    assert got.reference == f"{HOST}/cogs/cog-a@{row.digest}"
    assert got.present and got.indexed_at is not None
    assert store.known(SOURCE) == [
        KnownArtifact(repository="cogs/cog-a", digest=row.digest, tags=("v1",), status=STATUS_INDEXED, removed=False)
    ]


def test_upsert_replaces_the_row_whole_and_clears_removal(store):
    first = artifact("a", tags=("v1",))
    store.upsert(first)
    store.mark_removed_one(SOURCE, "cogs/cog-a", first.digest)
    assert not store.get(first.digest).present

    store.upsert(artifact("a", tags=("v1", "latest"), document=card(version="1.0.1")))

    got = store.get(first.digest)
    assert got.present and got.tags == ("latest", "v1") and got.version == "1.0.1"
    assert len(store.locations(first.digest)) == 1


def test_update_tags_changes_tags_only_and_restores_presence(store):
    row = artifact("a", tags=("v1",))
    store.upsert(row)
    store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest)

    assert store.update_tags(SOURCE, "cogs/cog-a", row.digest, ("latest", "v1")) is True
    got = store.get(row.digest)
    assert got.tags == ("latest", "v1") and got.present
    assert got.card == row.card and got.pushed_at == T0

    later = T0 + timedelta(hours=1)
    store.update_tags(SOURCE, "cogs/cog-a", row.digest, ("v1",), pushed_at=later)
    assert store.get(row.digest).pushed_at == later
    assert store.update_tags(SOURCE, "cogs/cog-a", digest("z"), ("v1",)) is False


def test_mark_removed_marks_only_absent_present_rows_and_never_deletes(store):
    keep = artifact("a")
    gone = artifact("b", repository="cogs/cog-b", document=card("example/cog-b"))
    other_source = artifact("c", source_id="other", repository="cogs/cog-c", document=card("example/cog-c"))
    for row in (keep, gone, other_source):
        store.upsert(row)

    assert store.mark_removed(SOURCE, {"cogs/cog-a": [keep.digest]}) == 1
    # Idempotent: already-removed rows are not counted again.
    assert store.mark_removed(SOURCE, {"cogs/cog-a": [keep.digest]}) == 0

    assert store.get(keep.digest).present
    removed = store.get(gone.digest)
    assert removed is not None and removed.removed_at is not None
    assert removed.card == card("example/cog-b"), "a removed row stays readable, card and all"
    assert store.get(other_source.digest).present, "another source's rows are untouched"
    assert {row.digest for row in store.list_current()} == {keep.digest, other_source.digest}
    assert store.list_versions("example/cog-b") == []
    assert [row.digest for row in store.list_versions("example/cog-b", include_removed=True)] == [gone.digest]


def test_mark_removed_one_reports_whether_it_did_anything(store):
    row = artifact("a")
    store.upsert(row)
    assert store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest) is True
    assert store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest) is False
    assert store.mark_removed_one(SOURCE, "cogs/cog-a", digest("z")) is False


def test_same_digest_in_two_repositories_is_two_rows_and_get_prefers_present(store):
    first = artifact("a", repository="cogs/cog-a")
    second = artifact("a", repository="mirror/cog-a-0001")
    store.upsert(first)
    store.upsert(second)

    assert [row.repository for row in store.locations(first.digest)] == ["cogs/cog-a", "mirror/cog-a-0001"]
    store.mark_removed_one(SOURCE, "cogs/cog-a", first.digest)
    assert store.get(first.digest).repository == "mirror/cog-a-0001"
    assert store.get(first.digest, repository="cogs/cog-a").removed_at is not None
    assert store.get(first.digest, source_id="nope") is None
    # One cog_id, one entry in the current list even across two locations.
    assert len(store.list_current()) == 1


def test_list_current_collapses_to_the_newest_per_cog_id(store):
    old = artifact("1", tags=("v1",), pushed_at=T0, document=card(version="1.0.0"))
    new = artifact("2", tags=("v2",), pushed_at=T0 + timedelta(days=1), document=card(version="2.0.0"))
    undated = artifact("3", tags=("dev",), pushed_at=None, document=card(version="3.0.0-dev"))
    for row in (undated, new, old):
        store.upsert(row)

    current = store.list_current()
    assert [row.version for row in current] == ["2.0.0"]
    versions = store.list_versions("example/cog-a")
    assert [row.version for row in versions] == ["2.0.0", "1.0.0", "3.0.0-dev"], "unknown pushed_at sorts last"


def test_list_current_excludes_non_cog_failed_and_idless_rows(store):
    store.upsert(artifact("a"))
    store.upsert(artifact("b", repository="images/nginx", status=STATUS_NON_COG, read_errors=("no COG.md",)))
    store.upsert(artifact("c", repository="cogs/broken", status=STATUS_FAILED, read_errors=("fetch: OCINotFound",)))
    draft = card()
    draft["id"] = None
    store.upsert(artifact("d", repository="cogs/draft", document=draft))

    assert [row.digest for row in store.list_current()] == [digest("a")]
    assert {row.status for row in (store.get(digest("b")), store.get(digest("c")))} == {STATUS_NON_COG, STATUS_FAILED}


def test_list_current_filters(store):
    store.upsert(
        artifact(
            "a",
            document=card(
                "example/transcriber",
                kind="complete",
                requires=["media/ffmpeg", "model-registry/example"],
                accepts=["media_transcription_request"],
                produces=["timestamped_transcript_bundle"],
            ),
        )
    )
    store.upsert(
        artifact(
            "b",
            repository="cogs/model",
            document=card("example/small-model", kind="model", publisher="Other", provides=["model-endpoint/openai"]),
        )
    )
    store.upsert(
        artifact("c", source_id="other", repository="cogs/notes", document=card("example/notes", kind="context"))
    )

    def ids(**filters) -> list[str]:
        return [row.cog_id for row in store.list_current(CatalogFilter(**filters))]

    assert ids() == ["example/notes", "example/small-model", "example/transcriber"]
    assert ids(kind="model") == ["example/small-model"]
    assert ids(publisher="Other") == ["example/small-model"]
    assert ids(source_id="other") == ["example/notes"]
    assert ids(requires="media/ffmpeg") == ["example/transcriber"]
    assert ids(requires="nothing/declares-this") == []
    assert ids(provides="model-endpoint/openai") == ["example/small-model"]
    assert ids(accepts="media_transcription_request") == ["example/transcriber"]
    assert ids(produces="timestamped_transcript_bundle") == ["example/transcriber"]
    assert ids(kind="complete", requires="model-registry/example") == ["example/transcriber"]
    assert ids(kind="model", requires="model-registry/example") == []


def test_list_current_filters_test_the_current_version_not_an_older_one(store):
    # v1 declared the capability and the kind; v2, the newest, dropped both.
    # Listing by them must not resurrect v1 as the Cog's "current" entry.
    store.upsert(
        artifact("1", pushed_at=T0, document=card(version="1.0.0", kind="model", requires=["gpu/any"])),
    )
    store.upsert(artifact("2", pushed_at=T0 + timedelta(days=1), document=card(version="2.0.0")))

    assert [row.version for row in store.list_current()] == ["2.0.0"]
    assert store.list_current(CatalogFilter(requires="gpu/any")) == []
    assert store.list_current(CatalogFilter(kind="model")) == []


def test_list_current_source_id_scopes_the_choice_of_newest(store):
    store.upsert(artifact("1", source_id="mirror", pushed_at=T0, document=card(version="1.0.0")))
    store.upsert(artifact("2", pushed_at=T0 + timedelta(days=1), document=card(version="2.0.0")))

    assert [row.version for row in store.list_current()] == ["2.0.0"]
    assert [row.version for row in store.list_current(CatalogFilter(source_id="mirror"))] == ["1.0.0"]


def test_list_current_q_matches_name_or_description_case_insensitively(store):
    described = card("example/transcriber")
    described["description"] = "Turns AUDIO into 100% timestamped text"
    store.upsert(artifact("a", repository="cogs/transcriber", document=described))
    store.upsert(artifact("b", repository="cogs/notes", document=card("example/notes_v2")))

    def ids(q: str) -> list[str]:
        return [row.cog_id for row in store.list_current(CatalogFilter(q=q))]

    assert ids("TRANSCRIBER") == ["example/transcriber"], "the name"
    assert ids("audio") == ["example/transcriber"], "the description"
    assert ids("100%") == ["example/transcriber"]
    assert ids("%") == ["example/transcriber"], "% is literal, not a wildcard"
    assert ids("s_v") == ["example/notes_v2"], "_ is literal, not a wildcard"
    assert ids("zzz") == []


def test_matches_query_only_reads_string_descriptions():
    assert matches_query(None, {"description": "Café au lait"}, "café")
    for description in (42, None, ["café"], {"text": "café"}, True):
        assert not matches_query(None, {"description": description}, "caf"), description
    assert not matches_query(None, None, "x")
    assert matches_query("cog-café", None, "CAF")


def test_like_pattern_escapes_wildcards_and_the_escape_character():
    assert like_pattern("a%b_c\\d") == "%a\\%b\\_c\\\\d%"


def test_list_current_pages_in_cog_id_order_with_offset(store):
    for index in range(5):
        store.upsert(artifact(f"{index}", repository=f"cogs/c{index}", document=card(f"example/c{index}")))

    def page(offset: int, limit: int = 2) -> list[str]:
        return [row.cog_id for row in store.list_current(limit=limit, offset=offset)]

    assert page(0) == ["example/c0", "example/c1"]
    assert page(2) == ["example/c2", "example/c3"]
    assert page(4) == ["example/c4"]
    assert page(5) == []
    with pytest.raises(ValueError):
        store.list_current(offset=-1)


def test_list_repositories_is_one_newest_present_cog_row_per_path(store):
    store.upsert(artifact("1", repository="cogs/a", pushed_at=T0, document=card("example/a", version="1")))
    store.upsert(
        artifact("2", repository="cogs/a", pushed_at=T0 + timedelta(days=1), document=card("example/a", version="2"))
    )
    # The same path in another source collapses into the same entry.
    store.upsert(artifact("3", source_id="mirror", repository="cogs/a", pushed_at=T0, document=card("example/a")))
    store.upsert(artifact("4", repository="cogs/b", document=card("example/b")))
    store.upsert(artifact("5", repository="cogs/gone", document=card("example/gone")))
    store.mark_removed_one(SOURCE, "cogs/gone", digest("5"))
    store.upsert(artifact("6", repository="images/nginx", status=STATUS_NON_COG))
    store.upsert(artifact("7", repository="cogs/broken", status=STATUS_FAILED))

    rows = store.list_repositories()
    assert [(row.repository, row.version) for row in rows] == [("cogs/a", "2"), ("cogs/b", "1.0.0")]


def test_list_current_limit_is_bounded_at_the_boundary(store):
    # MAX_LIST_LIMIT + 5 distinct cog_ids, so the cap is proven at its actual
    # boundary rather than inferred from a five-row set.
    for index in range(MAX_LIST_LIMIT + 5):
        store.upsert(artifact(f"{index:x}", repository=f"cogs/c{index}", document=card(f"example/c{index:04d}")))
    assert len(store.list_current(limit=2)) == 2
    listed = store.list_current(limit=MAX_LIST_LIMIT + 5)
    assert len(listed) == MAX_LIST_LIMIT
    with pytest.raises(ValueError):
        store.list_current(limit=0)


def test_upsert_refuses_an_unknown_status(store):
    with pytest.raises(ValueError, match="unknown catalog status"):
        store.upsert(artifact("a", status="vanished"))


def test_sweep_lock_is_non_blocking_and_single_holder(store):
    with store.sweep_lock() as view:
        assert view is store, "the in-memory store has no sessions: it is its own sweep view"
        with store.sweep_lock() as second:
            assert second is None
    with store.sweep_lock() as again:
        assert again is store


def test_unavailable_store_refuses_every_call():
    store = UnavailableCogCatalogStore()
    for call in (
        lambda: store.known(SOURCE),
        lambda: store.upsert(artifact("a")),
        lambda: store.update_tags(SOURCE, "r", digest("a"), ()),
        lambda: store.mark_removed(SOURCE, {}),
        lambda: store.mark_removed_one(SOURCE, "r", digest("a")),
        lambda: store.sweep_lock(),
        lambda: store.get(digest("a")),
        lambda: store.locations(digest("a")),
        lambda: store.list_current(),
        lambda: store.list_repositories(),
        lambda: store.list_versions("x"),
    ):
        with pytest.raises(CogCatalogUnavailableError):
            call()


CONTAINMENT_CASES = [
    # (document, needle, expected `document @> needle`). One shared table: the
    # in-memory helper is asserted against these expectations here, and the
    # live layer runs every case through a real server's `@>` so the two can
    # never quietly disagree (the codex gate caught exactly that: the helper
    # once let a scalar match a singleton array, which Postgres refuses).
    ({"a": 1, "b": 2}, {"a": 1}, True),
    ({"a": 1}, {"a": 2}, False),
    ({"a": 1}, {"b": 1}, False),
    (
        {"requires": [{"capability": "x", "locality": "any"}, {"capability": "y"}]},
        {"requires": [{"capability": "y"}]},
        True,
    ),
    ({"requires": [{"capability": "x"}]}, {"requires": [{"capability": "y"}]}, False),
    ({"provides": ["a", "b"]}, {"provides": ["b"]}, True),
    ({"provides": ["a", "b"]}, {"provides": ["b", "c"]}, False),
    ({"provides": []}, {"provides": []}, True),
    # Scalar/array asymmetry: below the top level, neither direction matches.
    ({"k": "v"}, {"k": ["v"]}, False),
    ({"k": ["v"]}, {"k": "v"}, False),
    # ... but a top-level array does contain a bare scalar (and not vice versa).
    (["v", "w"], "v", True),
    ("v", ["v"], False),
    # Booleans are not numbers, while numbers compare numerically.
    ({"k": 1}, {"k": True}, False),
    ({"k": True}, {"k": 1}, False),
    ({"k": 1}, {"k": 1.0}, True),
    # Nested arrays: an array element only matches an array element.
    ({"a": [[1, 2]]}, {"a": [[1]]}, True),
    ({"a": [[1]]}, {"a": [1]}, False),
    ({"a": [1, 2]}, {"a": [[1]]}, False),
    # Empty object/array and null.
    ({"a": [{"x": 1}]}, {"a": [{}]}, True),
    ({"a": None}, {"a": None}, True),
    ({"a": "x"}, {}, True),
    ({"io": {"accepts": ["x"]}}, {"io": {"accepts": ["x"]}}, True),
    ({"io": None}, {"io": {"accepts": ["x"]}}, False),
]


@pytest.mark.parametrize(("document", "needle", "expected"), CONTAINMENT_CASES)
def test_json_contains_mirrors_jsonb_containment(document, needle, expected):
    assert json_contains(document, needle) is expected


def test_get_prefers_the_most_recently_indexed_present_location(store):
    # Conflicting timestamps on purpose: the OLDER-pushed location was indexed
    # more recently, and get()'s documented order is by indexing recency --
    # every location of one digest shares the artifact's push time in
    # practice, so pushed_at cannot be the tiebreak.
    early, late = T0 - timedelta(days=1), T0 + timedelta(days=1)
    store.upsert(replace(artifact("a", repository="cogs/first", pushed_at=late), indexed_at=early))
    store.upsert(replace(artifact("a", repository="mirror/second", pushed_at=early), indexed_at=late))

    assert store.get(digest("a")).repository == "mirror/second"

    # A removed row loses to any present one, whatever its indexing recency.
    store.mark_removed_one(SOURCE, "mirror/second", digest("a"))
    assert store.get(digest("a")).repository == "cogs/first"


def test_naive_datetimes_are_refused(store):
    naive = T0.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.upsert(artifact("a", pushed_at=naive))
    store.upsert(artifact("a"))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.update_tags(SOURCE, "cogs/cog-a", digest("a"), ("v2",), pushed_at=naive)


def test_contains_nul_walks_values_and_keys_but_not_escape_text():
    assert contains_nul({"a": ["fine", {"b": "bad\x00"}]}) is True
    assert contains_nul({"bad\x00key": "v"}) is True
    assert contains_nul({"a": ["fine", {"b": r"literal \u0000 text"}]}) is False
    assert contains_nul({"n": 1, "b": True, "x": None}) is False


def test_cards_that_merely_mention_nul_are_stored(store):
    document = card()
    document["body"] = r"The escape sequence \u0000 denotes NUL."
    store.upsert(artifact("a", document=document))
    assert store.get(digest("a")).card["body"] == document["body"]


class _OffsetlessTz(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


def test_datetimes_with_a_tzinfo_but_no_offset_are_refused(store):
    # tzinfo present, utcoffset() None: naive in every way that matters
    # (round-2 codex finding).
    sneaky = T0.replace(tzinfo=_OffsetlessTz())
    with pytest.raises(ValueError, match="timezone-aware"):
        store.upsert(artifact("a", pushed_at=sneaky))
    store.upsert(artifact("a"))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.update_tags(SOURCE, "cogs/cog-a", digest("a"), ("v2",), pushed_at=sneaky)


def test_upsert_refuses_a_card_jsonb_cannot_store(store):
    # Parity with Postgres, where a NUL in any jsonb string is refused: the
    # in-memory store must fail the same way or tests would pass on content
    # production rejects (the codex gate's poison-card finding).
    poison = card()
    poison["summary"] = "before\x00after"
    with pytest.raises(CogCatalogDataError):
        store.upsert(artifact("a", document=poison))
    assert store.get(digest("a")) is None


def test_card_search_fields_stringify_scalars_and_drop_structures():
    fields = card_search_fields({"id": "a/b", "name": "b", "version": 1.5, "kind": "model", "publisher": {"x": 1}})
    assert fields == {
        "cog_id": "a/b",
        "name": "b",
        "version": "1.5",
        "kind": "model",
        "publisher": None,
        "manifest_schema": None,
    }


# ---------------------------------------------------------------------------
# Live-Postgres coverage (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres catalog tests",
)


def _database(max_size: int = 10):
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=max_size, timeout_seconds=10.0)


@pytest.fixture
def live_store():
    """A migrated live database with an empty catalog, dropped again afterwards."""

    from test_collab_schema import COLLAB_TABLES

    database = _database()

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        yield PostgresCogCatalogStore(database), database
        drop_all()
    finally:
        database.close()


@live_postgres
def test_live_upsert_get_update_tags_and_removal(live_store):
    store, _ = live_store
    row = artifact("a", tags=("v1",))
    store.upsert(row)

    got = store.get(row.digest)
    assert got.card == card(), "the card round-trips through jsonb verbatim"
    assert got.tags == ("v1",) and got.pushed_at == T0 and got.indexed_at is not None
    assert got.reference == f"{HOST}/cogs/cog-a@{row.digest}"
    assert got.read_errors == () and got.present
    first_indexed_at = got.indexed_at

    # Tag change: no card write (indexed_at unchanged), presence restored.
    store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest)
    assert store.update_tags(SOURCE, "cogs/cog-a", row.digest, ("latest", "v1")) is True
    got = store.get(row.digest)
    assert got.tags == ("latest", "v1") and got.present and got.indexed_at == first_indexed_at

    # Upsert replaces the row whole and refreshes indexed_at.
    store.upsert(artifact("a", tags=("v2",), document=card(version="2.0.0"), read_errors=("warning: x",)))
    got = store.get(row.digest)
    assert got.version == "2.0.0" and got.tags == ("v2",) and got.read_errors == ("warning: x",)
    assert got.indexed_at >= first_indexed_at
    assert len(store.locations(row.digest)) == 1

    # Removal keeps the row readable by digest.
    other = artifact("b", repository="cogs/cog-b", document=card("example/cog-b"))
    store.upsert(other)
    assert store.mark_removed(SOURCE, {"cogs/cog-a": [row.digest]}) == 1
    assert store.mark_removed(SOURCE, {"cogs/cog-a": [row.digest]}) == 0
    removed = store.get(other.digest)
    assert removed.removed_at is not None and removed.card == card("example/cog-b")
    assert [r.digest for r in store.list_current()] == [row.digest]
    assert [r.digest for r in store.list_versions("example/cog-b", include_removed=True)] == [other.digest]
    assert sorted(store.known(SOURCE), key=lambda k: k.repository) == [
        KnownArtifact("cogs/cog-a", row.digest, ("v2",), STATUS_INDEXED, False),
        KnownArtifact("cogs/cog-b", other.digest, ("v1",), STATUS_INDEXED, True),
    ]


@live_postgres
def test_live_status_check_and_non_cog_rows(live_store):
    import psycopg

    store, database = live_store
    store.upsert(artifact("n", repository="images/nginx", status=STATUS_NON_COG, read_errors=("no COG.md layer",)))
    got = store.get(digest("n"))
    assert got.status == STATUS_NON_COG and got.card is None and got.read_errors == ("no COG.md layer",)
    assert store.list_current() == []

    with pytest.raises(psycopg.errors.CheckViolation):
        with database.connection() as conn:
            conn.execute("UPDATE collab_cog_artifacts SET status = 'vanished'")


@live_postgres
def test_live_containment_filters_match_and_use_the_gin_index(live_store):
    from psycopg.types.json import Jsonb

    store, database = live_store
    store.upsert(
        artifact(
            "a",
            document=card(
                "example/transcriber",
                requires=["media/ffmpeg", "model-registry/example"],
                accepts=["media_transcription_request"],
                produces=["timestamped_transcript_bundle"],
            ),
        )
    )
    store.upsert(
        artifact(
            "b",
            repository="cogs/model",
            document=card("example/small-model", kind="model", publisher="Other", provides=["model-endpoint/openai"]),
        )
    )
    store.upsert(
        artifact(
            "c",
            repository="cogs/old",
            pushed_at=T0 - timedelta(days=1),
            document=card("example/transcriber", version="0.9", requires=["media/ffmpeg"]),
        )
    )

    def ids(**filters) -> list[str]:
        return [row.cog_id for row in store.list_current(CatalogFilter(**filters))]

    assert ids() == ["example/small-model", "example/transcriber"]
    assert ids(kind="model") == ["example/small-model"]
    assert ids(publisher="Other") == ["example/small-model"]
    assert ids(requires="media/ffmpeg") == ["example/transcriber"]
    assert ids(requires="nothing/declares-this") == []
    assert ids(provides="model-endpoint/openai") == ["example/small-model"]
    assert ids(accepts="media_transcription_request") == ["example/transcriber"]
    assert ids(produces="timestamped_transcript_bundle") == ["example/transcriber"]
    assert ids(kind="complete", requires="model-registry/example") == ["example/transcriber"]
    # Newest-per-cog_id collapse happens in SQL: the 0.9 row is the same cog_id, older.
    assert [row.version for row in store.list_versions("example/transcriber")] == ["1.0.0", "0.9"]

    # The filter is written as `card @> %s`, which is what the jsonb_path_ops
    # GIN index serves. On a three-row table the planner will not necessarily
    # pick that index over the removed_at btree (both are eligible and both
    # are cheap), so two things are proven separately: the store's statement
    # carries the containment predicate on `card`, and that predicate alone is
    # answered from the GIN index once a sequential scan is off the table.
    filters = CatalogFilter(requires="media/ffmpeg")
    sql, params = PostgresCogCatalogStore.current_query(filters, filters.containment(), 10)
    bound = [Jsonb(p) if isinstance(p, dict) else p for p in params]
    with database.connection() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(row["QUERY PLAN"] for row in conn.execute("EXPLAIN " + sql, bound).fetchall())
        bare = "\n".join(
            row["QUERY PLAN"]
            for row in conn.execute(
                "EXPLAIN SELECT digest FROM collab_cog_artifacts WHERE card @> %s", (Jsonb(filters.containment()),)
            ).fetchall()
        )
    assert '(card @> \'{"requires": [{"capability": "media/ffmpeg"}]}\'::jsonb)' in plan, plan
    assert "Index Scan" in plan and "Seq Scan" not in plan, plan
    assert "collab_cog_artifacts_card_idx" in bare, bare


def _exercise_read_api_listing(store) -> None:
    """The listing semantics the read API (#85) relies on, run against either backend."""

    # example/a: v1 required a capability and was a model; v2 (newest) is neither.
    store.upsert(
        artifact(
            "1",
            repository="cogs/a",
            pushed_at=T0,
            document=card("example/a", version="1", kind="model", requires=["gpu/any"]),
        )
    )
    store.upsert(
        artifact("2", repository="cogs/a", pushed_at=T0 + timedelta(days=1), document=card("example/a", version="2"))
    )
    # An older copy of example/a in another source, under the same path.
    store.upsert(
        artifact("3", source_id="mirror", repository="cogs/a", pushed_at=T0, document=card("example/a", version="1"))
    )
    described = card("example/B_tool")
    described["description"] = "Handles 100% of AUDIO"
    store.upsert(artifact("4", repository="cogs/B", document=described))
    store.upsert(artifact("5", repository="cogs/c", document=card("example/c")))
    store.upsert(artifact("6", repository="cogs/gone", document=card("example/gone")))
    store.mark_removed_one(SOURCE, "cogs/gone", digest("6"))
    store.upsert(artifact("7", repository="images/nginx", status=STATUS_NON_COG))
    # Structured descriptions never match q, whatever their text would be.
    for seed, description in (("8", ["café", "AUDIO"]), ("9", {"text": "café AUDIO"}), ("0", 1000)):
        odd = card(f"example/odd-{seed}")
        odd["description"] = description
        store.upsert(artifact(seed, repository=f"odd/o{seed}", document=odd))

    def ids(filters=None, **kwargs) -> list[str]:
        return [row.cog_id for row in store.list_current(filters, **kwargs)]

    odd = ["example/odd-0", "example/odd-8", "example/odd-9"]
    # Code-point order: "B" sorts before "a", whatever the database locale.
    assert ids() == ["example/B_tool", "example/a", "example/c", *odd]
    assert ids(CatalogFilter(q="café")) == []
    assert ids(CatalogFilter(q="100")) == ["example/B_tool"], "not the numeric description 1000"
    assert ids(CatalogFilter(q="odd-8")) == ["example/odd-8"], "the name still matches"
    assert [row.version for row in store.list_current(CatalogFilter(q="example/a"))] == []  # q is not the id
    assert ids(CatalogFilter(requires="gpu/any")) == [], "filters test the current version"
    assert ids(CatalogFilter(kind="model")) == []
    assert [row.version for row in store.list_current(CatalogFilter(source_id="mirror"))] == ["1"]
    assert ids(CatalogFilter(q="audio")) == ["example/B_tool"]
    assert ids(CatalogFilter(q="b_T")) == ["example/B_tool"]
    assert ids(CatalogFilter(q="0%")) == ["example/B_tool"]
    assert ids(CatalogFilter(q="%")) == ["example/B_tool"], "% is literal"
    assert ids(CatalogFilter(q="a_")) == [], "_ is literal"
    assert ids(limit=2) == ["example/B_tool", "example/a"]
    assert ids(limit=2, offset=2) == ["example/c", "example/odd-0"]
    assert ids(limit=2, offset=6) == []
    assert [(row.repository, row.version) for row in store.list_repositories()] == [
        ("cogs/B", "1.0.0"),
        ("cogs/a", "2"),
        ("cogs/c", "1.0.0"),
        ("odd/o0", "1.0.0"),
        ("odd/o8", "1.0.0"),
        ("odd/o9", "1.0.0"),
    ]


def test_read_api_listing_semantics_in_memory(store):
    _exercise_read_api_listing(store)


@live_postgres
def test_live_read_api_listing_semantics(live_store):
    store, _ = live_store
    _exercise_read_api_listing(store)


@live_postgres
def test_live_sweep_lock_is_single_flight_across_connections_and_restores_autocommit(live_store):
    store, database = live_store
    other = PostgresCogCatalogStore(_database(max_size=2))
    try:
        with store.sweep_lock() as held:
            assert held is not None
            with other.sweep_lock() as second:
                assert second is None, "a second connection must not get the session lock"
            # The lock is session-level: a fresh transaction on another
            # connection of the *same* pool must also be refused, and the
            # holder's connection must not be inside a transaction.
            with database.connection() as conn:
                row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (COG_INDEX_LOCK_KEY,)).fetchone()
                assert row["locked"] is False
                idle = conn.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity"
                    " WHERE state = 'idle in transaction' AND pid <> pg_backend_pid()"
                    " AND query LIKE '%pg_try_advisory_lock%'"
                ).fetchone()
                assert idle["n"] == 0, "the lock holder must not sit idle in a transaction"
        with other.sweep_lock() as after:
            assert after is not None
    finally:
        other._db.close()
    # Autocommit restoration is asserted with connection identity in
    # test_live_lock_connection_returns_with_autocommit_restored below; a
    # checkout from a many-connection pool here would not have proven it was
    # the lock's connection being inspected.


class _FakeLockConnection:
    """A psycopg-shaped connection for the lock helper's cleanup paths.

    ``fail_on`` names a statement prefix whose execution raises; the helper's
    contract under test is what happens to the connection afterwards.
    """

    def __init__(self, fail_on: str | None = None, *, fail_autocommit: bool = False):
        self.fail_on = fail_on
        self.fail_autocommit = fail_autocommit
        self._autocommit = False
        self.closed = False
        self.statements: list[str] = []

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if self.fail_autocommit:
            raise RuntimeError("injected failure on autocommit")
        self._autocommit = value

    def execute(self, sql, params=None):
        self.statements.append(sql.strip())
        if self.fail_on and sql.strip().startswith(self.fail_on):
            raise RuntimeError(f"injected failure on {self.fail_on}")
        return self

    def fetchone(self):
        return {"locked": True}

    def close(self):
        self.closed = True


class _FakeLockDb:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


def test_lock_connection_goes_back_clean_on_the_happy_path():
    from collab_hub_api.cogs.catalog import _postgres_sweep_lock

    conn = _FakeLockConnection()
    with _postgres_sweep_lock(_FakeLockDb(conn)) as view:
        assert view is not None and conn.autocommit is True
        assert view._conn is conn, "the view is bound to the lock's connection"
    assert not conn.closed
    assert conn.autocommit is False, "autocommit restored"
    assert [s.split("(")[0].split(" =")[0] for s in conn.statements] == [
        "SET statement_timeout",
        "SELECT pg_try_advisory_lock",
        "SELECT pg_advisory_unlock",
        "RESET statement_timeout",
    ]


@pytest.mark.parametrize(
    "fail_on",
    ["SELECT pg_advisory_unlock", "RESET statement_timeout", "SELECT pg_try_advisory_lock", "SET statement_timeout"],
)
def test_lock_connection_is_discarded_when_any_cleanup_step_fails(fail_on):
    # Round-3 codex finding: a failed unlock left a session still holding the
    # advisory lock, and a failed RESET left the altered timeout (and
    # autocommit) on a connection that then went back to the pool. Any step
    # whose outcome is uncertain now closes the connection, so the pool
    # discards it instead of handing it to the next borrower.
    from collab_hub_api.cogs.catalog import _postgres_sweep_lock

    conn = _FakeLockConnection(fail_on=fail_on)
    with pytest.raises(RuntimeError, match="injected failure"):
        with _postgres_sweep_lock(_FakeLockDb(conn)):
            pass
    assert conn.closed, f"a connection whose {fail_on} failed must not return to the pool"


def test_lock_connection_is_discarded_when_setting_autocommit_fails():
    # The one cleanup step that is not a statement: if the session cannot be
    # put into (or taken out of) autocommit, what the next borrower would
    # inherit is unknown, so the connection goes rather than the pool.
    from collab_hub_api.cogs.catalog import _postgres_sweep_lock

    conn = _FakeLockConnection(fail_autocommit=True)
    with pytest.raises(RuntimeError, match="injected failure on autocommit"):
        with _postgres_sweep_lock(_FakeLockDb(conn)):
            pass
    assert conn.closed and conn.statements == [], "it failed before any statement ran"


# ---------------------------------------------------------------------------
# The Postgres store against a fake connection.
#
# The live suites below are the real proof, but they are opt-in and CI has no
# server, so these pin the parts a server-less run can still check: that every
# method issues the statement it claims, with the parameters it claims, and
# maps rows back the way the API expects. They are what keeps the Postgres
# paths from being untested whenever the live suites are skipped.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Records every statement and replays canned rows, in order."""

    def __init__(self, results=()):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results)
        self.closed = False
        self.autocommit = False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append((text, tuple(params or ())))
        if text.startswith("SELECT set_config"):
            # The sweep connection's own preamble: it consumes no canned rows,
            # so a test's results line up with the statements it cares about.
            return _FakeResult([])
        return _FakeResult(self._results.pop(0) if self._results else [])

    def close(self):
        self.closed = True

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.calls]


class _FakeDb:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self, timeout=None):
        yield self.conn


DIGEST_A = digest("a")
DIGEST_B = digest("b")


def _fake_store(results=()):
    conn = _FakeConnection(results)
    return PostgresCogCatalogStore(_FakeDb(conn)), conn


def _row(**overrides) -> dict:
    row = {
        "source_id": SOURCE,
        "host": HOST,
        "repository": "cogs/cog-a",
        "digest": DIGEST_A,
        "tags": ["v1"],
        "pushed_at": T0,
        "indexed_at": T0,
        "manifest_media_type": None,
        "status": STATUS_INDEXED,
        "card": card(),
        "cog_id": "example/cog-a",
        "name": "cog-a",
        "version": "1",
        "kind": "complete",
        "publisher": "Example",
        "manifest_schema": "openteams/cog-manifest [0.1]",
        "read_errors": [],
        "removed_at": None,
    }
    row.update(overrides)
    return row


def test_sweep_path_statements_are_bounded_by_a_transaction_local_timeout():
    store, conn = _fake_store()
    store.known(SOURCE)
    first, params = conn.calls[0]
    assert first.startswith("SELECT set_config('statement_timeout'")
    assert params == (str(int(SWEEP_STATEMENT_TIMEOUT_SECONDS * 1000)),)
    assert "WHERE source_id = %s" in conn.statements[1]


def test_known_maps_rows_to_known_artifacts():
    rows = [
        {
            "repository": "cogs/a",
            "digest": DIGEST_A,
            "tags": ["v1", "latest"],
            "status": STATUS_INDEXED,
            "removed": False,
        },
        {"repository": "cogs/b", "digest": DIGEST_B, "tags": None, "status": STATUS_FAILED, "removed": True},
    ]
    store, _ = _fake_store([rows])
    known = store.known(SOURCE)
    assert [(k.repository, k.tags, k.status, k.removed) for k in known] == [
        ("cogs/a", ("v1", "latest"), STATUS_INDEXED, False),
        ("cogs/b", (), STATUS_FAILED, True),
    ]


def test_upsert_sends_sorted_tags_and_clears_removed_at():
    store, conn = _fake_store()
    store.upsert(artifact("a", tags=("v2", "v1", "v2")))
    sql, params = conn.calls[1]
    assert sql.startswith("INSERT INTO collab_cog_artifacts")
    assert "removed_at = NULL" in sql and "indexed_at = now()" in sql
    assert params[4] == ["v1", "v2"], "tags are deduplicated and sorted"


def test_upsert_refuses_an_unknown_status_before_touching_the_database():
    store, conn = _fake_store()
    with pytest.raises(ValueError, match="unknown catalog status"):
        store.upsert(replace(artifact("a"), status="invented"))
    assert conn.calls == []


def test_upsert_translates_a_psycopg_data_error():
    import psycopg

    class _Refusing(_FakeConnection):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.strip().startswith("INSERT"):
                raise psycopg.DataError("unsupported Unicode escape sequence")
            return _FakeResult([])

    conn = _Refusing()
    store = PostgresCogCatalogStore(_FakeDb(conn))
    with pytest.raises(CogCatalogDataError) as info:
        store.upsert(artifact("a"))
    # Class name only: the server's own message quotes the offending value.
    assert str(info.value) == "DataError"
    assert "unsupported" not in str(info.value)


def test_update_tags_refuses_a_naive_pushed_at_before_touching_the_database():
    from datetime import datetime

    store, conn = _fake_store()
    with pytest.raises(ValueError, match="pushed_at"):
        store.update_tags(SOURCE, "cogs/a", DIGEST_A, ["v1"], pushed_at=datetime(2026, 9, 1))
    assert conn.calls == []


def test_update_tags_reports_whether_a_row_existed():
    store, conn = _fake_store([[{"digest": DIGEST_A}]])
    assert store.update_tags(SOURCE, "cogs/a", DIGEST_A, ["b", "a", "b"], pushed_at=T0) is True
    sql, params = conn.calls[1]
    assert "SET tags = %s" in sql and "removed_at = NULL" in sql
    assert params[0] == ["a", "b"] and params[1] == T0

    store, _ = _fake_store([[]])
    assert store.update_tags(SOURCE, "cogs/a", DIGEST_A, ["v1"]) is False


def test_mark_removed_excludes_the_repositories_it_is_given():
    store, conn = _fake_store([[{"n": 3}]])
    marked = store.mark_removed(SOURCE, {"cogs/a": [DIGEST_A, DIGEST_A]}, excluding=["cogs/z", "cogs/z"])
    assert marked == 3
    sql, params = conn.calls[1]
    assert "NOT (a.repository = ANY(%s))" in sql
    assert params[0].obj == {"cogs/a": [DIGEST_A]}, "the present set is deduplicated and sorted"
    assert params[1] == SOURCE and params[2] == ["cogs/z"]


def test_mark_removed_answers_zero_without_a_row():
    store, _ = _fake_store([[]])
    assert store.mark_removed(SOURCE, {}) == 0


def test_mark_removed_one_only_marks_a_present_row():
    store, conn = _fake_store([[{"digest": DIGEST_A}]])
    assert store.mark_removed_one(SOURCE, "cogs/a", DIGEST_A) is True
    assert "removed_at IS NULL" in conn.statements[1]

    store, _ = _fake_store([[]])
    assert store.mark_removed_one(SOURCE, "cogs/a", DIGEST_A) is False


# ---------------------------------------------------------------------------
# Sweep writes ride the lock's session (issue #128), through the view the
# lock hands out and nothing else (issue #148)
# ---------------------------------------------------------------------------


class _LockAwareConnection(_FakeConnection):
    """A recording fake that also answers the sweep-lock helper's statements."""

    def __init__(self, results=(), *, locked: bool = True):
        super().__init__(results)
        self.locked = locked

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "advisory" in text or text.startswith(("SET ", "RESET ")):
            self.calls.append((text, tuple(params or ())))
            return _FakeResult([{"locked": self.locked}])
        return super().execute(sql, params)


class _FakeMultiDb:
    """Hands out a fresh recording connection per checkout, remembering each.

    Connection *identity* is what the #128 tests assert, so unlike ``_FakeDb``
    this never reuses one object. ``lock_answers`` scripts what successive
    connections answer to ``pg_try_advisory_lock`` (later checkouts answer
    ``True``, which only matters if they are asked).
    """

    def __init__(self, lock_answers=(True,)):
        self.connections: list[_LockAwareConnection] = []
        self._lock_answers = list(lock_answers)

    @contextmanager
    def connection(self, timeout=None):
        locked = self._lock_answers.pop(0) if self._lock_answers else True
        conn = _LockAwareConnection(locked=locked)
        self.connections.append(conn)
        yield conn


def test_sweep_writes_ride_the_lock_holding_session():
    # Issue #128: every sweep-path read and write goes through the view the
    # lock yields, and that view is bound to the lock's own connection -- so
    # a dead sweeper's writes are dropped by the server together with its
    # lock; there is no second session for a write to outlive the lock on.
    db = _FakeMultiDb()
    store = PostgresCogCatalogStore(db)
    with store.sweep_lock() as view:
        assert view is not None and view is not store, "the Postgres view is a connection-bound object, not the store"
        lock_conn = db.connections[0]
        view.known(SOURCE)
        view.upsert(artifact("a"))
        view.update_tags(SOURCE, "cogs/a", DIGEST_A, ["v1"])
        view.mark_removed(SOURCE, {"cogs/a": [DIGEST_A]})
        assert len(db.connections) == 1, "no call on the view checked out a second connection"
        assert not any(s.startswith("SELECT set_config") for s in lock_conn.statements), (
            "the lock session's timeout is session-set; a transaction-local set would be an autocommit no-op"
        )
        assert any(s.startswith("SELECT repository") for s in lock_conn.statements)
        assert any(s.startswith("INSERT INTO collab_cog_artifacts") for s in lock_conn.statements)
        assert any(s.startswith("UPDATE collab_cog_artifacts") for s in lock_conn.statements)
        assert any(s.startswith("WITH present AS") for s in lock_conn.statements)
    assert lock_conn.statements[-2].startswith("SELECT pg_advisory_unlock"), "released on the same connection"


def test_a_plain_store_call_during_a_sweep_uses_its_own_connection():
    # Issue #148: the store has no lock-connection attribute to consult, so a
    # call on the store itself while a sweep holds the lock -- the webhook's
    # targeted writes, a read, anything a future caller forgets to route
    # through the view -- checks out a pooled, statement-bounded connection of
    # its own. Nothing but the view can ride (or die with) the lock session.
    db = _FakeMultiDb()
    store = PostgresCogCatalogStore(db)
    assert not hasattr(store, "_lock_conn"), "the store publishes no lock state"
    with store.sweep_lock() as view:
        assert view is not None
        lock_conn = db.connections[0]
        store.known(SOURCE)
        store.upsert(artifact("a"))
        store.update_tags(SOURCE, "cogs/a", DIGEST_A, ["v1"])
        store.mark_removed(SOURCE, {"cogs/a": [DIGEST_A]})
        store.mark_removed_one(SOURCE, "cogs/cog-a", DIGEST_A)
        assert len(db.connections) == 6, "each plain store call checked out its own connection"
        for conn in db.connections[1:]:
            assert conn.statements[0].startswith("SELECT set_config('statement_timeout'"), "own, bounded"
            assert not any("advisory" in s for s in conn.statements), "and never touches the lock"
        assert not any(s.startswith(("SELECT repository", "INSERT", "UPDATE", "WITH")) for s in lock_conn.statements), (
            "nothing a plain store call did ran on the lock's session"
        )


def test_a_losing_sweep_lock_gets_no_view_and_leaves_the_winner_alone():
    db = _FakeMultiDb(lock_answers=[True, False])
    store = PostgresCogCatalogStore(db)
    with store.sweep_lock() as view:
        assert view is not None
        winner = db.connections[0]
        with store.sweep_lock() as second:
            assert second is None, "a loser gets no view: there is no session it may write on"
        assert not winner.closed and not any("unlock" in s for s in winner.statements), (
            "a loser's exit must not release the winner's lock"
        )
        view.known(SOURCE)
        assert len(db.connections) == 2 and winner.statements[-1].startswith("SELECT repository")


def test_get_prefers_present_rows_and_scopes_by_source_and_repository():
    store, conn = _fake_store([[_row()]])
    found = store.get(DIGEST_A, source_id=SOURCE, repository="cogs/cog-a")
    assert found.digest == DIGEST_A and found.cog_id == "example/cog-a"
    sql, params = conn.calls[0]
    assert "ORDER BY (removed_at IS NOT NULL), indexed_at DESC" in sql
    assert params == (DIGEST_A, SOURCE, SOURCE, "cogs/cog-a", "cogs/cog-a")

    store, _ = _fake_store([[]])
    assert store.get(DIGEST_A) is None


def test_locations_returns_every_row_for_a_digest():
    store, conn = _fake_store([[_row(), _row(repository="cogs/mirror")]])
    assert [a.repository for a in store.locations(DIGEST_A)] == ["cogs/cog-a", "cogs/mirror"]
    assert "ORDER BY source_id, repository" in conn.statements[0]


def test_list_current_builds_the_filters_it_is_given():
    store, conn = _fake_store([[_row()]])
    rows = store.list_current(CatalogFilter(kind="complete", publisher="Example", requires="gpu"), limit=5)
    assert [r.cog_id for r in rows] == ["example/cog-a"]
    sql, params = conn.calls[0]
    assert "DISTINCT ON (cog_id)" in sql and "card @> %s" in sql
    assert "removed_at IS NULL" in sql and "kind = %s" in sql and "publisher = %s" in sql
    assert params[-2:] == (5, 0), "the bounded limit, then the offset, close the parameters"


def test_list_current_collapses_before_it_filters_and_pages_in_code_point_order():
    store, conn = _fake_store([[]])
    filters = CatalogFilter(source_id="mirror", kind="model", provides="x", q="50%")
    store.list_current(filters, limit=3, offset=6)
    sql, params = conn.calls[0]
    inner, _, outer = sql.partition(") AS current_cogs")
    # source_id scopes the choice of the newest row; the rest test that row.
    assert "source_id = %s" in inner and "kind = %s" not in inner
    assert "kind = %s" in outer and "card @> %s" in outer
    # The card filter is also a GIN-served candidate prefilter inside.
    assert "cog_id IN (SELECT cog_id FROM collab_cog_artifacts WHERE card @> %s)" in inner
    assert (
        "(name ILIKE %s ESCAPE %s OR (jsonb_typeof(card->'description') = 'string'"
        " AND card->>'description' ILIKE %s ESCAPE %s))"
    ) in outer
    assert "'\\'" not in sql, "the escape character is bound, not a literal"
    assert 'ORDER BY cog_id COLLATE "C" LIMIT %s OFFSET %s' in outer
    needle = {"provides": ["x"]}
    unwrapped = tuple(getattr(param, "obj", param) for param in params)  # Jsonb wraps the needle
    pattern = "%50\\%%"
    assert unwrapped == ("mirror", needle, "model", needle, pattern, "\\", pattern, "\\", 3, 6)


def test_list_current_without_filters_still_excludes_removed_rows():
    store, conn = _fake_store([[]])
    assert store.list_current() == []
    assert "removed_at IS NULL" in conn.statements[0] and "card @>" not in conn.statements[0]
    assert "WHERE TRUE" in conn.statements[0]


def test_list_current_refuses_a_negative_offset_before_touching_the_database():
    store, conn = _fake_store()
    with pytest.raises(ValueError):
        store.list_current(offset=-1)
    assert conn.calls == []


def test_list_repositories_is_one_present_cog_row_per_path_in_code_point_order():
    store, conn = _fake_store([[_row(repository="cogs/b"), _row(repository="cogs/B")]])
    assert [row.repository for row in store.list_repositories()] == ["cogs/B", "cogs/b"]
    sql = conn.statements[0]
    assert "DISTINCT ON (repository)" in sql and "removed_at IS NULL" in sql and "cog_id IS NOT NULL" in sql


def test_list_versions_orders_newest_first_and_can_include_removed():
    store, conn = _fake_store([[_row(version="2"), _row(version="1")]])
    assert [a.version for a in store.list_versions("example/cog-a", include_removed=True)] == ["2", "1"]
    sql, params = conn.calls[0]
    assert "ORDER BY pushed_at DESC NULLS LAST, indexed_at DESC" in sql
    assert params == ("example/cog-a", True)


@live_postgres
def test_live_lock_connection_is_discarded_when_the_backend_dies_mid_sweep(live_store):
    """The unlock fails because the server killed the session: the pool must not reuse it.

    Terminating the lock holder's backend is the realistic version of "the
    unlock did not succeed": on exit the helper's cleanup raises, the
    connection is broken, and the one-connection pool's next checkout is a
    fresh backend (a different pid) with default transaction semantics.
    """

    import psycopg

    _, database = live_store
    single = _database(max_size=1)
    store = PostgresCogCatalogStore(single)
    try:
        with pytest.raises(psycopg.OperationalError):
            with store.sweep_lock() as held:
                assert held is not None
                with database.connection() as conn:
                    row = conn.execute(
                        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
                        (COG_INDEX_LOCK_KEY >> 32, COG_INDEX_LOCK_KEY & 0xFFFFFFFF),
                    ).fetchone()
                    holder_pid = row["pid"]
                    conn.execute("SELECT pg_terminate_backend(%s)", (holder_pid,))
        with single.connection() as conn:
            assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] != holder_pid
            assert conn.autocommit is False
            assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"
        with store.sweep_lock() as held:
            assert held is not None, "the lock died with the terminated session"
    finally:
        single.close()


@live_postgres
def test_live_lock_connection_returns_with_autocommit_restored(live_store):
    """The very connection that held the lock goes back with autocommit off.

    A one-connection pool makes the identity provable: the next checkout MUST
    be the connection that held the lock (the pid, read out of pg_locks while
    it was held, pins that), so the transaction semantics asserted afterwards
    are asserted on the right connection rather than on whichever one a larger
    pool happened to hand out.
    """

    _, database = live_store
    single = _database(max_size=1)
    store = PostgresCogCatalogStore(single)
    try:
        with store.sweep_lock() as held:
            assert held is not None
            with database.connection() as conn:
                row = conn.execute(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
                    (COG_INDEX_LOCK_KEY >> 32, COG_INDEX_LOCK_KEY & 0xFFFFFFFF),
                ).fetchone()
            assert row is not None
            holder_pid = row["pid"]

        with pytest.raises(RuntimeError):
            with single.connection() as conn:
                assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == holder_pid
                assert conn.autocommit is False
                conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES ('rolled-back', 'sub')")
                raise RuntimeError("abort")
        with single.connection() as conn:
            assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == holder_pid
            assert conn.execute("SELECT count(*) AS n FROM collab_orgs").fetchone()["n"] == 0
    finally:
        single.close()


@live_postgres
def test_live_jsonb_containment_agrees_with_the_helper(live_store):
    """Every shared containment case, asked of a real server and of the helper.

    Agreement is the property (the in-memory store must answer filters exactly
    as production does); the expected values in the table are additionally
    pinned so a wrong expectation cannot hide a double failure.
    """

    from psycopg.types.json import Jsonb

    _, database = live_store
    with database.connection() as conn:
        for document, needle, expected in CONTAINMENT_CASES:
            row = conn.execute("SELECT %s::jsonb @> %s::jsonb AS contains", (Jsonb(document), Jsonb(needle))).fetchone()
            assert row["contains"] is expected, f"server disagrees with the table for {document!r} @> {needle!r}"
            assert json_contains(document, needle) is expected, f"helper disagrees for {document!r} @> {needle!r}"


@live_postgres
def test_live_poison_card_is_a_data_error_not_an_outage(live_store):
    """psycopg's DataError for NUL-in-jsonb surfaces as CogCatalogDataError.

    This is the second line behind the indexer's pre-validation: bypass the
    validation (write straight to the store) and the translation still lets a
    caller distinguish "this row's content" from "the database is down".
    """

    store, _ = live_store
    poison = card()
    poison["summary"] = "nul\x00nul"
    with pytest.raises(CogCatalogDataError):
        store.upsert(artifact("f", repository="cogs/poison", document=poison))
    assert store.get(digest("f")) is None

    with pytest.raises(ValueError, match="timezone-aware"):
        store.upsert(artifact("f", pushed_at=T0.replace(tzinfo=None)))


@live_postgres
def test_live_get_orders_by_indexing_recency_not_push_time(live_store):
    store, _ = live_store
    # The later-pushed location is indexed FIRST; the earlier-pushed one is
    # indexed second and must win, because get() is about this catalog's own
    # recency, not the artifact's shared push time.
    store.upsert(artifact("a", repository="cogs/first", pushed_at=T0 + timedelta(days=1)))
    time.sleep(0.01)  # separate the two server-side indexed_at values
    store.upsert(artifact("a", repository="mirror/second", pushed_at=T0 - timedelta(days=1)))

    assert store.get(digest("a")).repository == "mirror/second"
    store.mark_removed_one(SOURCE, "mirror/second", digest("a"))
    assert store.get(digest("a")).repository == "cogs/first", "a present row beats a removed one"


@live_postgres
def test_live_concurrent_sweepers_exactly_one_wins(live_store):
    _, _ = live_store
    replicas = 6
    stores = [PostgresCogCatalogStore(_database(max_size=2)) for _ in range(replicas)]
    start = threading.Barrier(replicas)
    release = threading.Event()

    def attempt(store: PostgresCogCatalogStore) -> bool:
        start.wait()
        with store.sweep_lock() as held:
            if held is not None:
                release.wait(timeout=10)
            return held is not None

    try:
        with ThreadPoolExecutor(max_workers=replicas) as pool:
            futures = [pool.submit(attempt, store) for store in stores]
            # Let the losers report before the winner lets go.
            while sum(f.done() for f in futures) < replicas - 1:
                threading.Event().wait(0.01)
            release.set()
            results = [f.result() for f in futures]
    finally:
        for store in stores:
            store._db.close()
    assert results.count(True) == 1 and results.count(False) == replicas - 1


@live_postgres
def test_live_sweep_writes_share_the_lock_sessions_backend(live_store):
    """Issue #128, live: the sweep's write session IS the lock session.

    Asserted by backend identity (``pg_backend_pid``), which is what "same
    session" means server-side -- a write on that backend is dropped with the
    lock if the sweeper dies. The targeted path is asserted to use a
    *different* backend, so a webhook write lands concurrently with a sweep.
    """

    store, database = live_store
    with store.sweep_lock() as view:
        assert view is not None
        with database.connection() as conn:
            row = conn.execute(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
                (COG_INDEX_LOCK_KEY >> 32, COG_INDEX_LOCK_KEY & 0xFFFFFFFF),
            ).fetchone()
        holder_pid = row["pid"]
        assert view._conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == holder_pid
        with store._own_connection() as conn:
            assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] != holder_pid
        # A write on the lock session commits statement by statement
        # (autocommit) and is visible to other sessions mid-sweep.
        view.upsert(artifact("a"))
        assert store.get(digest("a")) is not None, "get() reads on an ordinary pooled connection"


@live_postgres
def test_live_sweep_write_cannot_apply_once_the_lock_session_is_dead(live_store):
    """Kill the lock backend mid-sweep: the next sweep write fails instead of landing.

    The crash window of issue #128, exercised from the lock's side: the moment
    the server drops the lock session, another replica can acquire the lock --
    and the old sweeper's write, riding that dead session, can no longer apply.
    """

    import psycopg

    store, database = live_store
    other = PostgresCogCatalogStore(_database(max_size=2))
    try:
        with pytest.raises(psycopg.Error):
            with other.sweep_lock() as view:
                assert view is not None
                with database.connection() as conn:
                    row = conn.execute(
                        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
                        (COG_INDEX_LOCK_KEY >> 32, COG_INDEX_LOCK_KEY & 0xFFFFFFFF),
                    ).fetchone()
                    conn.execute("SELECT pg_terminate_backend(%s)", (row["pid"],))
                # The server freed the lock with the session: another replica
                # can take it (polled -- termination is asynchronous)...
                deadline = time.monotonic() + 10
                acquired = False
                while not acquired:
                    with store.sweep_lock() as taken:
                        acquired = taken is not None
                    assert acquired or time.monotonic() < deadline, "the freed lock was never acquirable"
                # ...while the old sweeper's write rides the dead session and
                # dies with it instead of landing under the new holder's lock.
                view.upsert(artifact("a"))
        assert store.get(digest("a")) is None, "the fenced write did not land"
    finally:
        other._db.close()
