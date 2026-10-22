import base64
import json
from datetime import UTC, datetime, timedelta


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(user: str, workspace: str = "workspace-a") -> dict[str, str]:
    return {"IdToken-test": _jwt({"preferred_username": user, "org_id": "org-a", "workspace_id": workspace})}


def task_payload() -> dict:
    return {
        "title": "Daily briefing",
        "prompt": "Prepare my briefing",
        "execution_device_id": "device-a",
        "agent_id": "agent-a",
        "schedule": {"kind": "daily", "local_time": "09:00"},
        "time_zone": "America/New_York",
        "requirements": {"local_files": ["/briefing"]},
    }


async def test_task_crud_is_owner_and_workspace_scoped(client):
    created_response = await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=task_payload())
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["revision"] == 1
    assert created["schedule_revision"] == 1
    assert created["local_only"] is True
    assert created["catch_up_window_seconds"] == 3600
    assert created["concurrency_policy"] == "skip_if_running"

    assert (await client.get("/v1/tasks", cookies=auth_cookie("bob"))).json() == []
    assert (await client.get("/v1/tasks", cookies=auth_cookie("alice", "workspace-b"))).json() == []

    updated_response = await client.patch(
        f"/v1/tasks/{created['id']}",
        cookies=auth_cookie("alice"),
        json={"revision": 1, "title": "Updated briefing"},
    )
    assert updated_response.status_code == 200
    updated = updated_response.json()
    assert updated["revision"] == 2
    assert updated["schedule_revision"] == 1

    conflict = await client.patch(
        f"/v1/tasks/{created['id']}",
        cookies=auth_cookie("alice"),
        json={"revision": 1, "title": "Stale edit"},
    )
    assert conflict.status_code == 409

    deleted = await client.delete(f"/v1/tasks/{created['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204


async def test_task_patch_rejects_blank_required_strings(client):
    task = (await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=task_payload())).json()

    response = await client.patch(
        f"/v1/tasks/{task['id']}",
        cookies=auth_cookie("alice"),
        json={"revision": 1, "title": "   "},
    )

    assert response.status_code == 422


async def test_client_assigned_task_id_is_preserved_for_offline_replication(client):
    payload = task_payload()
    payload["id"] = "task_local-stable-id"
    created = await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=payload)
    assert created.status_code == 201
    assert created.json()["id"] == "task_local-stable-id"
    duplicate = await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=payload)
    assert duplicate.status_code == 409


def _recent(seconds: int = 0) -> str:
    """A run's creation time a few minutes ago, plus ``seconds``: runs expire 90 days after creation."""
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


async def test_run_occurrence_is_idempotent_and_history_survives_task_delete(client):
    task = (await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=task_payload())).json()
    # A recent run: a run expires 90 days after it was created, so a fixed date stops being listed.
    recent = _recent()
    run_request = {
        "task_revision": task["revision"],
        "schedule_revision": task["schedule_revision"],
        "created_at": recent,
        "occurrence_key": f"{task['id']}:1:{recent}",
        "trigger": "scheduled",
        "scheduled_at": recent,
        "execution_device_id": "device-a",
        "agent_id": "agent-a",
        "task_snapshot": task,
    }
    first = await client.post(f"/v1/tasks/{task['id']}/runs", cookies=auth_cookie("alice"), json=run_request)
    second = await client.post(f"/v1/tasks/{task['id']}/runs", cookies=auth_cookie("alice"), json=run_request)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert datetime.fromisoformat(first.json()["created_at"].replace("Z", "+00:00")) == datetime.fromisoformat(
        recent.replace("Z", "+00:00")
    )

    run_id = first.json()["id"]
    running = await client.patch(
        f"/v1/tasks/runs/{run_id}",
        cookies=auth_cookie("alice"),
        json={"status": "running", "harness_environment_id": "hermes-docker"},
    )
    assert running.status_code == 200

    notifications = await client.get("/v1/task-notifications", cookies=auth_cookie("alice"))
    assert notifications.json()[0]["kind"] == "run_running"
    notification_id = notifications.json()[0]["id"]
    marked = await client.post(
        "/v1/task-notifications/mark-read",
        cookies=auth_cookie("alice"),
        json={"ids": [notification_id]},
    )
    assert marked.json() == {"updated": 1}

    assert (await client.delete(f"/v1/tasks/{task['id']}", cookies=auth_cookie("alice"))).status_code == 204
    history = await client.get("/v1/task-runs", cookies=auth_cookie("alice"))
    assert [item["id"] for item in history.json()] == [run_id]


async def test_device_heartbeat_uses_five_minute_grace(client):
    response = await client.post(
        "/v1/task-devices/heartbeat",
        cookies=auth_cookie("alice"),
        json={"device_id": "device-a", "display_name": "Alice laptop", "capabilities": ["docker"]},
    )
    assert response.status_code == 200
    device = response.json()
    assert device["device_id"] == "device-a"
    assert device["expires_at"] > device["last_seen_at"]
    listed = await client.get("/v1/task-devices", cookies=auth_cookie("alice"))
    assert [item["device_id"] for item in listed.json()] == ["device-a"]


async def test_unsupported_concurrency_policy_is_rejected(client):
    payload = task_payload()
    payload["concurrency_policy"] = "run_concurrently"
    response = await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=payload)
    assert response.status_code == 422


async def test_storage_policy_and_run_retention_are_bounded(client):
    unauthenticated = await client.get("/v1/tasks/storage-policy")
    assert unauthenticated.status_code == 401

    policy = await client.get("/v1/tasks/storage-policy", cookies=auth_cookie("alice"))
    assert policy.status_code == 200
    assert policy.json() == {
        "max_log_bytes_per_run": 25 * 1024 * 1024,
        "max_artifact_bytes_per_run": 500 * 1024 * 1024,
        "max_total_task_storage_bytes": 5 * 1024 * 1024 * 1024,
        "max_retained_runs_per_task": 100,
    }

    task = (await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=task_payload())).json()
    for index in range(101):
        response = await client.post(
            f"/v1/tasks/{task['id']}/runs",
            cookies=auth_cookie("alice"),
            json={
                "task_revision": 1,
                "schedule_revision": 1,
                "occurrence_key": f"occurrence-{index}",
                "trigger": "scheduled",
                "execution_device_id": "device-a",
                "agent_id": "agent-a",
                "task_snapshot": task,
            },
        )
        assert response.status_code == 201

    history = await client.get(f"/v1/tasks/{task['id']}/runs", cookies=auth_cookie("alice"))
    assert len(history.json()) == 100


async def test_run_lists_support_offset(client):
    task = (await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=task_payload())).json()
    for index in range(3):
        response = await client.post(
            f"/v1/tasks/{task['id']}/runs",
            cookies=auth_cookie("alice"),
            json={
                "id": f"run_{index}",
                "created_at": _recent(index),
                "task_revision": 1,
                "schedule_revision": 1,
                "occurrence_key": f"offset-{index}",
                "trigger": "scheduled",
                "execution_device_id": "device-a",
                "agent_id": "agent-a",
                "task_snapshot": task,
            },
        )
        assert response.status_code == 201

    global_page = await client.get("/v1/task-runs?limit=1&offset=1", cookies=auth_cookie("alice"))
    task_page = await client.get(f"/v1/tasks/{task['id']}/runs?limit=1&offset=1", cookies=auth_cookie("alice"))

    assert [item["id"] for item in global_page.json()] == ["run_1"]
    assert [item["id"] for item in task_page.json()] == ["run_1"]


async def test_full_delete_removes_run_history(client):
    task = (await client.post("/v1/tasks", cookies=auth_cookie("alice"), json=task_payload())).json()
    await client.post(
        f"/v1/tasks/{task['id']}/runs",
        cookies=auth_cookie("alice"),
        json={
            "task_revision": 1,
            "schedule_revision": 1,
            "occurrence_key": "full-delete",
            "trigger": "manual",
            "execution_device_id": "device-a",
            "agent_id": "agent-a",
            "task_snapshot": task,
        },
    )
    response = await client.delete(f"/v1/tasks/{task['id']}?delete_history=true", cookies=auth_cookie("alice"))
    assert response.status_code == 204
    assert (await client.get("/v1/task-runs", cookies=auth_cookie("alice"))).json() == []
