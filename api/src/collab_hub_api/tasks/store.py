from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta
from threading import RLock
from typing import Any, Callable, TypeVar

import psycopg

from ..frames.db import locked_schema_connection
from .models import (
    MAX_RETAINED_RUNS_PER_TASK,
    DeviceHeartbeat,
    DeviceRecord,
    NotificationRecord,
    TaskCreate,
    TaskPatch,
    TaskRecord,
    TaskRunCreate,
    TaskRunPatch,
    TaskRunRecord,
    new_id,
    utc_now,
)


class TaskNotFoundError(KeyError):
    pass


class TaskConflictError(RuntimeError):
    pass


ScopeKey = tuple[str, str, str]
TaskKey = tuple[str, str, str, str]
RunKey = tuple[str, str, str, str]
OccurrenceKey = tuple[str, str, str, str, str]

logger = logging.getLogger(__name__)


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._tasks: dict[TaskKey, TaskRecord] = {}
        self._runs: dict[RunKey, TaskRunRecord] = {}
        self._occurrences: dict[OccurrenceKey, RunKey] = {}
        self._devices: dict[tuple[str, str, str, str], DeviceRecord] = {}
        self._notifications: dict[RunKey, NotificationRecord] = {}

    @staticmethod
    def _scoped_key(org_id: str, workspace_id: str, owner_id: str, resource_id: str) -> TaskKey:
        return org_id, workspace_id, owner_id, resource_id

    @staticmethod
    def _record_key(record: TaskRecord | TaskRunRecord | NotificationRecord) -> TaskKey:
        return record.org_id, record.workspace_id, record.owner_id, record.id

    @staticmethod
    def _occurrence_key(record: TaskRunRecord) -> OccurrenceKey:
        return record.org_id, record.workspace_id, record.owner_id, record.task_id, record.occurrence_key

    def _prune_expired_runs(self) -> bool:
        now = utc_now()
        expired_keys = {key for key, item in self._runs.items() if item.expires_at <= now}
        if not expired_keys:
            return False
        for run_key in expired_keys:
            item = self._runs.pop(run_key)
            self._occurrences.pop(self._occurrence_key(item), None)
        self._notifications = {
            key: value
            for key, value in self._notifications.items()
            if value.run_id is None
            or self._scoped_key(value.org_id, value.workspace_id, value.owner_id, value.run_id) not in expired_keys
        }
        return True

    def list_tasks(self, org_id: str, workspace_id: str, owner_id: str) -> list[TaskRecord]:
        with self._lock:
            return deepcopy(
                sorted(
                    (
                        item
                        for item in self._tasks.values()
                        if item.org_id == org_id and item.workspace_id == workspace_id and item.owner_id == owner_id
                    ),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )
            )

    def create_task(self, org_id: str, workspace_id: str, owner_id: str, request: TaskCreate) -> TaskRecord:
        now = utc_now()
        record = TaskRecord(
            **request.model_dump(exclude={"id"}),
            id=request.id or new_id("task"),
            org_id=org_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            local_only=True,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            key = self._record_key(record)
            if key in self._tasks:
                raise TaskConflictError(record.id)
            self._tasks[key] = record
        return deepcopy(record)

    def get_task(self, org_id: str, workspace_id: str, owner_id: str, task_id: str) -> TaskRecord:
        with self._lock:
            item = self._tasks.get(self._scoped_key(org_id, workspace_id, owner_id, task_id))
            if item is None:
                raise TaskNotFoundError(task_id)
            return deepcopy(item)

    def update_task(
        self, org_id: str, workspace_id: str, owner_id: str, task_id: str, request: TaskPatch
    ) -> TaskRecord:
        with self._lock:
            current = self.get_task(org_id, workspace_id, owner_id, task_id)
            if current.revision != request.revision:
                raise TaskConflictError(task_id)
            changes = request.model_dump(exclude={"revision"}, exclude_unset=True)
            schedule_fields = {"schedule", "time_zone", "missed_run_policy", "catch_up_window_seconds"}
            schedule_changed = any(name in changes for name in schedule_fields)
            updated = current.model_copy(
                update={
                    **changes,
                    "revision": current.revision + 1,
                    "schedule_revision": current.schedule_revision + (1 if schedule_changed else 0),
                    "updated_at": utc_now(),
                }
            )
            self._tasks[self._scoped_key(org_id, workspace_id, owner_id, task_id)] = updated
            return deepcopy(updated)

    def delete_task(self, org_id: str, workspace_id: str, owner_id: str, task_id: str, delete_history: bool) -> None:
        with self._lock:
            self.get_task(org_id, workspace_id, owner_id, task_id)
            del self._tasks[self._scoped_key(org_id, workspace_id, owner_id, task_id)]
            if delete_history:
                run_keys = [
                    key
                    for key, run in self._runs.items()
                    if key[:3] == (org_id, workspace_id, owner_id) and run.task_id == task_id
                ]
                for run_key in run_keys:
                    run = self._runs.pop(run_key)
                    self._occurrences.pop(self._occurrence_key(run), None)
                self._notifications = {
                    key: value
                    for key, value in self._notifications.items()
                    if key[:3] != (org_id, workspace_id, owner_id) or value.task_id != task_id
                }

    def list_runs(
        self,
        org_id: str,
        workspace_id: str,
        owner_id: str,
        task_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRunRecord]:
        with self._lock:
            self._prune_expired_runs()
            items = [
                item
                for item in self._runs.values()
                if item.org_id == org_id
                and item.workspace_id == workspace_id
                and item.owner_id == owner_id
                and (task_id is None or item.task_id == task_id)
            ]
            return deepcopy(sorted(items, key=lambda item: item.created_at, reverse=True)[offset : offset + limit])

    def list_task_runs(
        self, org_id: str, workspace_id: str, owner_id: str, task_id: str, limit: int = 100, offset: int = 0
    ) -> list[TaskRunRecord]:
        with self._lock:
            self.get_task(org_id, workspace_id, owner_id, task_id)
            return self.list_runs(org_id, workspace_id, owner_id, task_id, limit, offset)

    def create_run(
        self, org_id: str, workspace_id: str, owner_id: str, task_id: str, request: TaskRunCreate
    ) -> TaskRunRecord:
        self.get_task(org_id, workspace_id, owner_id, task_id)
        with self._lock:
            occurrence = (org_id, workspace_id, owner_id, task_id, request.occurrence_key)
            existing_key = self._occurrences.get(occurrence)
            if existing_key is not None:
                return deepcopy(self._runs[existing_key])
            now = utc_now()
            created_at = request.created_at or now
            record = TaskRunRecord(
                **request.model_dump(exclude={"id", "created_at"}),
                id=request.id or new_id("run"),
                task_id=task_id,
                org_id=org_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                created_at=created_at,
                updated_at=now,
                expires_at=created_at + timedelta(days=90),
                logs_expires_at=created_at + timedelta(days=30),
                artifacts_expires_at=created_at + timedelta(days=90),
            )
            run_key = self._record_key(record)
            if run_key in self._runs:
                raise TaskConflictError(record.id)
            self._runs[run_key] = record
            self._occurrences[occurrence] = run_key

            # Enforce retention on write so unattended tasks stay bounded.
            task_runs = sorted(
                (
                    item
                    for key, item in self._runs.items()
                    if key[:3] == (org_id, workspace_id, owner_id) and item.task_id == task_id
                ),
                key=lambda item: item.created_at,
                reverse=True,
            )
            for expired in task_runs[MAX_RETAINED_RUNS_PER_TASK:]:
                self._runs.pop(self._record_key(expired), None)
                self._occurrences.pop(self._occurrence_key(expired), None)
            return deepcopy(record)

    def update_run(
        self, org_id: str, workspace_id: str, owner_id: str, run_id: str, request: TaskRunPatch
    ) -> TaskRunRecord:
        with self._lock:
            run_key = self._scoped_key(org_id, workspace_id, owner_id, run_id)
            current = self._runs.get(run_key)
            if current is None:
                raise TaskNotFoundError(run_id)
            updated = current.model_copy(update={**request.model_dump(exclude_unset=True), "updated_at": utc_now()})
            self._runs[run_key] = updated
            if updated.status != current.status and updated.status in {
                "running",
                "succeeded",
                "failed",
                "needs_reauthorization",
            }:
                notification = NotificationRecord(
                    id=new_id("notification"),
                    org_id=org_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    task_id=updated.task_id,
                    run_id=updated.id,
                    kind=f"run_{updated.status}",
                    detail={"reason": updated.reason} if updated.reason else {},
                    created_at=utc_now(),
                )
                self._notifications[self._record_key(notification)] = notification
            return deepcopy(updated)

    def heartbeat(self, org_id: str, workspace_id: str, user_id: str, request: DeviceHeartbeat) -> DeviceRecord:
        now = utc_now()
        record = DeviceRecord(
            **request.model_dump(),
            org_id=org_id,
            workspace_id=workspace_id,
            user_id=user_id,
            last_seen_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        with self._lock:
            self._devices[(org_id, workspace_id, user_id, request.device_id)] = record
        return deepcopy(record)

    def list_devices(self, org_id: str, workspace_id: str, user_id: str) -> list[DeviceRecord]:
        with self._lock:
            now = utc_now()
            expired_keys = [key for key, item in self._devices.items() if item.expires_at <= now]
            for key in expired_keys:
                self._devices.pop(key, None)
            return deepcopy([item for key, item in self._devices.items() if key[:3] == (org_id, workspace_id, user_id)])

    def create_notification(
        self,
        org_id: str,
        workspace_id: str,
        owner_id: str,
        task_id: str,
        kind: str,
        run_id: str | None = None,
        detail: dict | None = None,
    ) -> NotificationRecord:
        record = NotificationRecord(
            id=new_id("notification"),
            org_id=org_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            detail=detail or {},
            created_at=utc_now(),
        )
        with self._lock:
            self._notifications[self._record_key(record)] = record
        return deepcopy(record)

    def list_notifications(
        self, org_id: str, workspace_id: str, owner_id: str, unread_only: bool = False
    ) -> list[NotificationRecord]:
        with self._lock:
            return deepcopy(
                sorted(
                    (
                        item
                        for item in self._notifications.values()
                        if item.org_id == org_id
                        and item.workspace_id == workspace_id
                        and item.owner_id == owner_id
                        and (not unread_only or item.read_at is None)
                    ),
                    key=lambda item: item.created_at,
                    reverse=True,
                )
            )

    def mark_notifications_read(self, org_id: str, workspace_id: str, owner_id: str, ids: list[str]) -> int:
        changed = 0
        now = utc_now()
        with self._lock:
            for notification_id in ids:
                key = self._scoped_key(org_id, workspace_id, owner_id, notification_id)
                item = self._notifications.get(key)
                if item is not None and item.read_at is None:
                    self._notifications[key] = item.model_copy(update={"read_at": now})
                    changed += 1
        return changed


T = TypeVar("T")


class PostgresTaskStore:
    """Durable task state with one transactionally locked document per owner.

    The document boundary matches the authorization boundary and keeps task,
    run, and notification updates atomic. Ephemeral devices live separately so
    heartbeats do not rewrite durable task state.
    """

    def __init__(self, db, auto_migrate: bool = False) -> None:
        self.db = db
        self.url = db.database_url
        if auto_migrate:
            self._migrate()

    def _connect(self):
        # A transaction-scoped checkout from the shared pool — never a fresh
        # per-request psycopg.connect.
        return self.db.connection()

    def _ensure_schema(self) -> None:
        # Advisory-locked (issue #42): bare CREATE TABLE IF NOT EXISTS races
        # under concurrent replica startup.
        with locked_schema_connection(self.db) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_task_state (
                    org_id text NOT NULL,
                    workspace_id text NOT NULL,
                    owner_id text NOT NULL,
                    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (org_id, workspace_id, owner_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_task_devices (
                    org_id text NOT NULL,
                    workspace_id text NOT NULL,
                    user_id text NOT NULL,
                    device_id text NOT NULL,
                    payload jsonb NOT NULL,
                    last_seen_at timestamptz NOT NULL,
                    expires_at timestamptz NOT NULL,
                    PRIMARY KEY (org_id, workspace_id, user_id, device_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS nexus_task_devices_expiry_idx
                ON nexus_task_devices (expires_at)
                """
            )

    def _migrate(self) -> None:
        self._ensure_schema()

    @staticmethod
    def _dump(store: InMemoryTaskStore) -> dict[str, Any]:
        return {
            "tasks": [item.model_dump(mode="json") for item in store._tasks.values()],
            "runs": [item.model_dump(mode="json") for item in store._runs.values()],
            "devices": [item.model_dump(mode="json") for item in store._devices.values()],
            "notifications": [item.model_dump(mode="json") for item in store._notifications.values()],
        }

    @staticmethod
    def _load_records(
        values: list[Any],
        model: type[TaskRecord] | type[TaskRunRecord] | type[DeviceRecord] | type[NotificationRecord],
        label: str,
    ):
        records = []
        for value in values:
            try:
                records.append(model.model_validate(value))
            except Exception as exc:
                logger.warning("Skipping invalid stored task %s record: %s", label, exc)
        return records

    @staticmethod
    def _load(payload: dict[str, Any] | None) -> InMemoryTaskStore:
        store = InMemoryTaskStore()
        payload = payload or {}
        tasks = PostgresTaskStore._load_records(payload.get("tasks", []), TaskRecord, "task")
        runs = PostgresTaskStore._load_records(payload.get("runs", []), TaskRunRecord, "run")
        devices = PostgresTaskStore._load_records(payload.get("devices", []), DeviceRecord, "device")
        notifications = PostgresTaskStore._load_records(
            payload.get("notifications", []), NotificationRecord, "notification"
        )
        store._tasks = {store._record_key(item): item for item in tasks}
        store._runs = {store._record_key(item): item for item in runs}
        store._occurrences = {store._occurrence_key(item): store._record_key(item) for item in store._runs.values()}
        store._devices = {
            (item.org_id, item.workspace_id, item.user_id, item.device_id): item
            for item in devices
        }
        store._notifications = {store._record_key(item): item for item in notifications}
        return store

    def _with_state(
        self,
        org_id: str,
        workspace_id: str,
        owner_id: str,
        mutate: bool,
        operation: Callable[[InMemoryTaskStore], T],
        write_if_changed: bool = False,
    ) -> T:
        with self._connect() as connection, connection.cursor() as cursor:
            if mutate:
                cursor.execute(
                    """
                    INSERT INTO nexus_task_state (org_id, workspace_id, owner_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (org_id, workspace_id, owner_id),
                )
            cursor.execute(
                """
                SELECT payload FROM nexus_task_state
                WHERE org_id = %s AND workspace_id = %s AND owner_id = %s
                """
                + (" FOR UPDATE" if mutate else ""),
                (org_id, workspace_id, owner_id),
            )
            row = cursor.fetchone()
            store = self._load(row["payload"] if row else None)
            before = self._dump(store) if mutate and write_if_changed else None
            result = operation(store)
            if mutate and (not write_if_changed or before != self._dump(store)):
                cursor.execute(
                    """
                    UPDATE nexus_task_state SET payload = %s, updated_at = now()
                    WHERE org_id = %s AND workspace_id = %s AND owner_id = %s
                    """,
                    (psycopg.types.json.Jsonb(self._dump(store)), org_id, workspace_id, owner_id),
                )
            return result

    def list_tasks(self, org_id: str, workspace_id: str, owner_id: str):
        return self._with_state(
            org_id, workspace_id, owner_id, False, lambda store: store.list_tasks(org_id, workspace_id, owner_id)
        )

    def create_task(self, org_id: str, workspace_id: str, owner_id: str, request: TaskCreate):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.create_task(org_id, workspace_id, owner_id, request),
        )

    def get_task(self, org_id: str, workspace_id: str, owner_id: str, task_id: str):
        return self._with_state(
            org_id, workspace_id, owner_id, False, lambda store: store.get_task(org_id, workspace_id, owner_id, task_id)
        )

    def update_task(self, org_id: str, workspace_id: str, owner_id: str, task_id: str, request: TaskPatch):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.update_task(org_id, workspace_id, owner_id, task_id, request),
        )

    def delete_task(self, org_id: str, workspace_id: str, owner_id: str, task_id: str, delete_history: bool):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.delete_task(org_id, workspace_id, owner_id, task_id, delete_history),
        )

    def list_runs(
        self,
        org_id: str,
        workspace_id: str,
        owner_id: str,
        task_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.list_runs(org_id, workspace_id, owner_id, task_id, limit, offset),
            write_if_changed=True,
        )

    def list_task_runs(
        self, org_id: str, workspace_id: str, owner_id: str, task_id: str, limit: int = 100, offset: int = 0
    ):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.list_task_runs(org_id, workspace_id, owner_id, task_id, limit, offset),
            write_if_changed=True,
        )

    def create_run(self, org_id: str, workspace_id: str, owner_id: str, task_id: str, request: TaskRunCreate):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.create_run(org_id, workspace_id, owner_id, task_id, request),
        )

    def update_run(self, org_id: str, workspace_id: str, owner_id: str, run_id: str, request: TaskRunPatch):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.update_run(org_id, workspace_id, owner_id, run_id, request),
        )

    def heartbeat(self, org_id: str, workspace_id: str, user_id: str, request: DeviceHeartbeat):
        record = InMemoryTaskStore().heartbeat(org_id, workspace_id, user_id, request)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO nexus_task_devices (
                    org_id, workspace_id, user_id, device_id, payload, last_seen_at, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, workspace_id, user_id, device_id)
                DO UPDATE SET
                    payload = EXCLUDED.payload,
                    last_seen_at = EXCLUDED.last_seen_at,
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    org_id,
                    workspace_id,
                    user_id,
                    request.device_id,
                    psycopg.types.json.Jsonb(record.model_dump(mode="json")),
                    record.last_seen_at,
                    record.expires_at,
                ),
            )
        return record

    def list_devices(self, org_id: str, workspace_id: str, user_id: str):
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM nexus_task_devices
                WHERE org_id = %s AND workspace_id = %s AND user_id = %s
                    AND expires_at <= now()
                """,
                (org_id, workspace_id, user_id),
            )
            rows = connection.execute(
                """
                SELECT payload FROM nexus_task_devices
                WHERE org_id = %s AND workspace_id = %s AND user_id = %s
                ORDER BY last_seen_at DESC, device_id DESC
                """,
                (org_id, workspace_id, user_id),
            ).fetchall()
        devices = []
        for row in rows:
            try:
                devices.append(DeviceRecord.model_validate(row["payload"]))
            except Exception as exc:
                logger.warning("Skipping invalid stored task device record: %s", exc)
        return devices

    def list_legacy_devices(self, org_id: str, workspace_id: str, user_id: str):
        return self._with_state(
            org_id, workspace_id, user_id, False, lambda store: store.list_devices(org_id, workspace_id, user_id)
        )

    def create_notification(
        self,
        org_id: str,
        workspace_id: str,
        owner_id: str,
        task_id: str,
        kind: str,
        run_id: str | None = None,
        detail: dict | None = None,
    ):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.create_notification(org_id, workspace_id, owner_id, task_id, kind, run_id, detail),
        )

    def list_notifications(self, org_id: str, workspace_id: str, owner_id: str, unread_only: bool = False):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            False,
            lambda store: store.list_notifications(org_id, workspace_id, owner_id, unread_only),
        )

    def mark_notifications_read(self, org_id: str, workspace_id: str, owner_id: str, ids: list[str]):
        return self._with_state(
            org_id,
            workspace_id,
            owner_id,
            True,
            lambda store: store.mark_notifications_read(org_id, workspace_id, owner_id, ids),
        )
