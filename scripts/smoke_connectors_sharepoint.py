"""Smoke assertions for the Collab Hub SharePoint connector REST contract.

Run against a Collab Hub API configured to talk to scripts/testdata/fake_graph_app.py
(``connectors.sharepoint.api_base_url`` = ``http://127.0.0.1:8976/v1.0`` and either
``static_access_token`` = the fake's token or ``broker_token_url`` =
``http://127.0.0.1:8976/broker/token``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json

import httpx

# The fake provider's Graph token. It must never appear in any connector response.
FAKE_GRAPH_TOKEN = "eyJ-fake-graph-access-token"
DRIVE_ID = "b!fakeDrive-AbC123_xyz"
DOCX_ID = "01FAKEDOCX0000000000000000000001"
TXT_ID = "01FAKETXT00000000000000000000002"
PNG_ID = "01FAKEPNG00000000000000000000003"


def make_bearer_token(user: str, org: str, workspace: str) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'preferred_username': user, 'org_id': org, 'workspace_id': workspace})}."


async def smoke_sharepoint_connector(
    base_url: str, *, bearer_token: str | None
) -> None:
    token = bearer_token or make_bearer_token(
        "sp-smoke-user", "sp-smoke-org", "sp-smoke-workspace"
    )
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), timeout=30
    ) as unauthenticated_client:
        unauthenticated = await unauthenticated_client.get(
            "/v1/connectors/sharepoint/status"
        )
        assert unauthenticated.status_code == 401

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=30
    ) as client:
        status = await client.get("/v1/connectors/sharepoint/status")
        status.raise_for_status()
        status_payload = status.json()
        assert status_payload["connected"] is True
        assert status_payload["state"] == "connected"
        assert status_payload["account"] == "Fake User"
        assert status_payload["scopes"] == [
            "User.Read",
            "Sites.Read.All",
            "Files.Read.All",
        ]
        assert FAKE_GRAPH_TOKEN not in status.text

        search = await client.post(
            "/v1/connectors/sharepoint/search",
            json={"query": "marketing", "limit": 2, "time_zone": "UTC"},
        )
        search.raise_for_status()
        search_payload = search.json()
        assert search_payload["content_trust"] == "external_untrusted"
        # The personal-OneDrive hit is excluded by default, so page one has one hit.
        assert [hit["name"] for hit in search_payload["hits"]] == [
            "Q3 Marketing Plan.docx"
        ]
        assert search_payload["hits"][0]["site_name"] == "Marketing"
        assert search_payload["hits"][0]["summary"] == "Marketing plan for Q3 ..."
        assert search_payload["total"] == 4
        assert search_payload["next_page_token"]
        # URLs, OData links and markup are dropped and the token never leaks.
        assert "sharepoint.example" not in search.text
        assert "@odata" not in search.text
        assert "<c0>" not in search.text
        assert FAKE_GRAPH_TOKEN not in search.text

        page_two = await client.post(
            "/v1/connectors/sharepoint/search",
            json={
                "query": "marketing",
                "limit": 2,
                "time_zone": "UTC",
                "page_token": search_payload["next_page_token"],
            },
        )
        page_two.raise_for_status()
        assert [hit["name"] for hit in page_two.json()["hits"]] == [
            "notes.txt",
            "logo.png",
        ]
        assert page_two.json()["next_page_token"] == ""

        read_txt = await client.post(
            f"/v1/connectors/sharepoint/drives/{DRIVE_ID}/items/{TXT_ID}/read", json={}
        )
        read_txt.raise_for_status()
        txt_payload = read_txt.json()
        assert txt_payload["unsupported"] is False
        assert "Action item: Alice sends the report by Friday." in txt_payload["text"]
        assert "evil.example" not in read_txt.text
        assert FAKE_GRAPH_TOKEN not in read_txt.text

        read_docx = await client.post(
            f"/v1/connectors/sharepoint/drives/{DRIVE_ID}/items/{DOCX_ID}/read", json={}
        )
        read_docx.raise_for_status()
        docx_payload = read_docx.json()
        assert docx_payload["unsupported"] is False
        assert "Hello SharePoint from the fake converter" in docx_payload["text"]

        read_png = await client.post(
            f"/v1/connectors/sharepoint/drives/{DRIVE_ID}/items/{PNG_ID}/read", json={}
        )
        read_png.raise_for_status()
        png_payload = read_png.json()
        assert png_payload["unsupported"] is True
        assert png_payload["item"]["name"] == "logo.png"
        assert png_payload["text"] == ""

        missing = await client.post(
            f"/v1/connectors/sharepoint/drives/{DRIVE_ID}/items/01NOPE/read", json={}
        )
        assert missing.status_code == 404
        assert "SECRET-REQ-ID" not in missing.text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL")
    parser.add_argument("--bearer-token", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        smoke_sharepoint_connector(args.base_url, bearer_token=args.bearer_token)
    )


if __name__ == "__main__":
    main()
