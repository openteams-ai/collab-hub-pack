import os
from datetime import timedelta

import pytest

import collab_hub_api.tasks.store as task_store_module
from collab_hub_api.tasks.models import DeviceHeartbeat, TaskCreate, TaskRunCreate, utc_now
from collab_hub_api.tasks.store import InMemoryTaskStore, PostgresTaskStore


def test_expired_history_and_notifications_are_cleaned_up() -> None:
    store = InMemoryTaskStore()
    task = store.create_task(
        "org",
        "workspace",
        "owner",
        TaskCreate(
            title="Task",
            prompt="Run",
            execution_device_id="device",
            agent_id="agent",
            schedule={"kind": "manual"},
            time_zone="UTC",
        ),
    )
    run = store.create_run(
        "org",
        "workspace",
        "owner",
        task.id,
        TaskRunCreate(
            task_revision=1,
            schedule_revision=1,
            occurrence_key="expired",
            trigger="manual",
            execution_device_id="device",
            agent_id="agent",
            task_snapshot={},
        ),
    )
    store.create_notification("org", "workspace", "owner", task.id, "run_succeeded", run.id)
    store._runs[store._record_key(run)] = run.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})

    assert store.list_runs("org", "workspace", "owner") == []
    assert store.list_notifications("org", "workspace", "owner") == []


def test_client_ids_and_occurrences_are_scoped_to_owner_workspace() -> None:
    store = InMemoryTaskStore()
    task_request = TaskCreate(
        id="task_same",
        title="Task",
        prompt="Run",
        execution_device_id="device",
        agent_id="agent",
        schedule={"kind": "manual"},
        time_zone="UTC",
    )
    run_request = TaskRunCreate(
        id="run_same",
        task_revision=1,
        schedule_revision=1,
        occurrence_key="occurrence_same",
        trigger="manual",
        execution_device_id="device",
        agent_id="agent",
        task_snapshot={},
    )

    workspace_a = store.create_task("org", "workspace-a", "owner", task_request)
    workspace_b = store.create_task("org", "workspace-b", "owner", task_request)
    owner_b = store.create_task("org", "workspace-a", "owner-b", task_request)
    run_a = store.create_run("org", "workspace-a", "owner", workspace_a.id, run_request)
    run_b = store.create_run("org", "workspace-b", "owner", workspace_b.id, run_request)
    run_owner_b = store.create_run("org", "workspace-a", "owner-b", owner_b.id, run_request)

    assert [item.id for item in store.list_tasks("org", "workspace-a", "owner")] == ["task_same"]
    assert [item.id for item in store.list_tasks("org", "workspace-b", "owner")] == ["task_same"]
    assert [item.id for item in store.list_tasks("org", "workspace-a", "owner-b")] == ["task_same"]
    assert run_a.id == run_b.id == run_owner_b.id == "run_same"
    assert len(store._occurrences) == 3
    assert len(store.list_runs("org", "workspace-a", "owner")) == 1
    assert len(store.list_runs("org", "workspace-b", "owner")) == 1
    assert len(store.list_runs("org", "workspace-a", "owner-b")) == 1


def test_expired_devices_are_pruned_from_availability(monkeypatch) -> None:
    store = InMemoryTaskStore()
    device = store.heartbeat(
        "org",
        "workspace",
        "owner",
        DeviceHeartbeat(device_id="device", display_name="Laptop"),
    )
    monkeypatch.setattr(task_store_module, "utc_now", lambda: device.expires_at)

    assert store.list_devices("org", "workspace", "owner") == []
    assert store._devices == {}


def test_run_created_at_is_preserved_for_ordering_and_retention() -> None:
    store = InMemoryTaskStore()
    task = store.create_task(
        "org",
        "workspace",
        "owner",
        TaskCreate(
            title="Task",
            prompt="Run",
            execution_device_id="device",
            agent_id="agent",
            schedule={"kind": "manual"},
            time_zone="UTC",
        ),
    )
    newer = utc_now()
    older = newer - timedelta(days=1)
    store.create_run(
        "org",
        "workspace",
        "owner",
        task.id,
        TaskRunCreate(
            id="run_old",
            created_at=older,
            task_revision=1,
            schedule_revision=1,
            occurrence_key="old",
            trigger="manual",
            execution_device_id="device",
            agent_id="agent",
            task_snapshot={},
        ),
    )
    store.create_run(
        "org",
        "workspace",
        "owner",
        task.id,
        TaskRunCreate(
            id="run_new",
            created_at=newer,
            task_revision=1,
            schedule_revision=1,
            occurrence_key="new",
            trigger="manual",
            execution_device_id="device",
            agent_id="agent",
            task_snapshot={},
        ),
    )

    assert [item.id for item in store.list_runs("org", "workspace", "owner")] == ["run_new", "run_old"]


def test_list_runs_supports_offset() -> None:
    store = InMemoryTaskStore()
    task = store.create_task(
        "org",
        "workspace",
        "owner",
        TaskCreate(
            title="Task",
            prompt="Run",
            execution_device_id="device",
            agent_id="agent",
            schedule={"kind": "manual"},
            time_zone="UTC",
        ),
    )
    base = utc_now()
    for index in range(3):
        store.create_run(
            "org",
            "workspace",
            "owner",
            task.id,
            TaskRunCreate(
                id=f"run_{index}",
                created_at=base + timedelta(seconds=index),
                task_revision=1,
                schedule_revision=1,
                occurrence_key=f"occurrence-{index}",
                trigger="manual",
                execution_device_id="device",
                agent_id="agent",
                task_snapshot={},
            ),
        )

    assert [item.id for item in store.list_runs("org", "workspace", "owner", limit=1, offset=1)] == ["run_1"]


def test_postgres_payload_loader_skips_invalid_records(caplog) -> None:
    valid = InMemoryTaskStore().create_task(
        "org",
        "workspace",
        "owner",
        TaskCreate(
            title="Task",
            prompt="Run",
            execution_device_id="device",
            agent_id="agent",
            schedule={"kind": "manual"},
            time_zone="UTC",
        ),
    )

    loaded = PostgresTaskStore._load(
        {
            "tasks": [
                valid.model_dump(mode="json"),
                {"id": "bad", "title": "missing required fields"},
            ],
            "runs": [{"id": "bad-run"}],
        }
    )

    assert [item.id for item in loaded.list_tasks("org", "workspace", "owner")] == [valid.id]
    assert "Skipping invalid stored task task record" in caplog.text
    assert "Skipping invalid stored task run record" in caplog.text


def test_postgres_task_store_round_trip_against_real_database() -> None:
    url = os.getenv("COLLAB_HUB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("COLLAB_HUB_TEST_POSTGRES_URL is not set")

    # PostgresTaskStore takes a pooled PostgresDatabase, not a URL string.
    # This test kept passing a string and had been silently
    # skipped everywhere — CI provisions no Postgres — until the collab
    # migration work gave CI a database.
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(url)
    try:
        _postgres_task_store_round_trip(database)
    finally:
        database.close()


def _postgres_task_store_round_trip(database) -> None:
    store = PostgresTaskStore(database, auto_migrate=True)
    task = store.create_task(
        "org-roundtrip",
        "workspace",
        "owner",
        TaskCreate(
            title="Task",
            prompt="Run",
            execution_device_id="device",
            agent_id="agent",
            schedule={"kind": "manual"},
            time_zone="UTC",
        ),
    )
    run = store.create_run(
        "org-roundtrip",
        "workspace",
        "owner",
        task.id,
        TaskRunCreate(
            id="run_roundtrip",
            created_at=utc_now() - timedelta(minutes=5),
            task_revision=1,
            schedule_revision=1,
            occurrence_key="roundtrip",
            trigger="manual",
            execution_device_id="device",
            agent_id="agent",
            task_snapshot={},
        ),
    )
    store.heartbeat("org-roundtrip", "workspace", "owner", DeviceHeartbeat(device_id="device"))

    assert [item.id for item in store.list_tasks("org-roundtrip", "workspace", "owner")] == [task.id]
    assert [item.id for item in store.list_runs("org-roundtrip", "workspace", "owner")] == [run.id]
    assert [item.device_id for item in store.list_devices("org-roundtrip", "workspace", "owner")] == ["device"]

    store.delete_task("org-roundtrip", "workspace", "owner", task.id, delete_history=True)
    assert store.list_runs("org-roundtrip", "workspace", "owner") == []
