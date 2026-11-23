"""Smoke assertions for the Collab Hub Slack connector REST contract."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json

import httpx


def make_bearer_token(user: str, org: str, workspace: str) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}.{encode({'preferred_username': user, 'org_id': org, 'workspace_id': workspace})}."
    )


async def smoke_slack_connector(base_url: str, *, bearer_token: str | None) -> None:
    token = bearer_token or make_bearer_token("slack-smoke-user", "slack-smoke-org", "slack-smoke-workspace")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20) as unauthenticated_client:
        unauthenticated = await unauthenticated_client.get("/v1/connectors/slack/status")
        assert unauthenticated.status_code == 401

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=20) as client:
        status = await client.get("/v1/connectors/slack/status")
        status.raise_for_status()
        status_payload = status.json()
        assert status_payload["connected"] is True
        assert status_payload["state"] == "connected"
        assert "channels:history" in status_payload["scopes"]
        assert "im:history" not in status_payload["scopes"]
        assert "mpim:history" not in status_payload["scopes"]
        assert all("write" not in scope and "post" not in scope for scope in status_payload["scopes"])
        assert "fake-slack-access-token" not in status.text

        channels = await client.get("/v1/connectors/slack/channels")
        channels.raise_for_status()
        channels_payload = channels.json()
        assert channels_payload["channels"][0]["id"] == "C0001"
        assert channels_payload["channels"][0]["name"] == "proposals"
        assert "fake-slack-access-token" not in channels.text

        search = await client.post(
            "/v1/connectors/slack/search",
            json={"query": "healthcare kickoff", "limit": 5},
        )
        search.raise_for_status()
        search_payload = search.json()
        assert [hit["channel_id"] for hit in search_payload["hits"]] == ["C0001"]
        assert all(not hit.get("is_im") and not hit.get("is_mpim") for hit in search_payload["hits"])
        assert "fake-slack-access-token" not in search.text

        dm_read = await client.post("/v1/connectors/slack/channels/D0001/read", json={"limit": 10})
        assert dm_read.status_code == 403
        assert "fake-slack-access-token" not in dm_read.text

        read = await client.post(
            "/v1/connectors/slack/channels/C0001/read",
            json={"limit": 10},
        )
        read.raise_for_status()
        read_payload = read.json()
        assert read_payload["channel_id"] == "C0001"
        assert len(read_payload["messages"]) == 2
        assert read_payload["messages"][1]["text"] == "Do we have healthcare kickoff notes?"
        assert "fake-slack-access-token" not in read.text
        # Link sanitization (apollo-desktop#365): the source message embeds a Slack
        # link entity and a plain URL; the connector must return link-free text.
        assert read_payload["messages"][0]["text"] == "Kickoff notes in the kickoff doc; mirror at [link]"
        assert "http://" not in read.text
        assert "https://" not in read.text
        # Default-valued fields such as has_more: false are left out (#139);
        # the untrusted-content marking is always sent.
        assert not read_payload.get("has_more")
        assert read_payload["content_trust"] == "external_untrusted"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL")
    parser.add_argument("--bearer-token", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(smoke_slack_connector(args.base_url, bearer_token=args.bearer_token))


if __name__ == "__main__":
    main()
