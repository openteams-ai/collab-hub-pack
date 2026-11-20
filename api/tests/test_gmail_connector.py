from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response

from collab_hub_api.config import Config
from collab_hub_api.connectors.gmail_client import (
    _decode_body,
    _message_content_info,
    _recipient_headers,
    _snippet_is_repeated,
)
from collab_hub_api.connectors.models import GmailMessageMetadata, GmailReadResponse, GmailSearchResponse
from collab_hub_api.core import make_app


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _auth_header() -> dict[str, str]:
    token = _jwt(
        {
            "preferred_username": "alice",
            "org_id": "org-a",
            "workspace_id": "workspace-a",
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "connectors": {
                "google": {
                    "static_access_token": "google-token-alice",
                    "drive_api_base_url": "https://google.test/drive/v3",
                    "gmail_api_base_url": "https://google.test/gmail/v1",
                }
            },
        }
    )


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


async def test_gmail_status_search_and_read_are_live_read_only_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    methods: list[str] = []

    def handler(request: httpx.Request) -> Response:
        methods.append(request.method)
        path = request.url.path
        if path.endswith("/users/me/messages"):
            if request.url.params.get("maxResults") == "1":
                assert request.url.params["q"] == "newer_than:1d"
                return Response(200, json={"messages": []})
            assert request.url.params["pageToken"] == "gmail-page-2"
            return Response(
                200,
                json={
                    "messages": [{"id": "msg-1", "threadId": "thread-1"}],
                    "resultSizeEstimate": 1,
                },
            )
        if path.endswith("/users/me/messages/msg-1") and request.url.params.get("format") == "metadata":
            return Response(
                200,
                json={
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "snippet": "Review https://unsafe.example.test/plan",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Connector plan"},
                            {"name": "From", "value": "Mark <mark@example.com>"},
                            {"name": "To", "value": "alice@example.com"},
                        ]
                    },
                },
            )
        if path.endswith("/users/me/messages/msg-1") and request.url.params.get("format") == "full":
            return Response(
                200,
                json={
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "payload": {
                        "headers": [{"name": "Subject", "value": "Connector plan"}],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "filename": "",
                                "body": {
                                    "data": _encoded("The rollout is approved. See https://unsafe.example.test/task")
                                },
                            },
                            {
                                "mimeType": "application/pdf",
                                "filename": "private.pdf",
                                "body": {"data": _encoded("PRIVATE ATTACHMENT CONTENT")},
                            },
                        ],
                    },
                },
            )
        return Response(404, json={"error": {"message": "not found"}})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = await client.get("/v1/connectors/gmail/status", headers=_auth_header())
            search = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "connector", "page_token": "gmail-page-2"},
            )
            read = await client.post(
                "/v1/connectors/gmail/messages/msg-1/read",
                headers=_auth_header(),
                json={"max_chars": 1000},
            )

    assert status.json()["state"] == "connected"
    assert search.status_code == 200
    assert search.json()["messages"][0]["snippet"] == "Review [link]"
    assert search.json()["content_trust"] == "external_untrusted"
    assert read.status_code == 200
    assert read.json()["text"] == "The rollout is approved. See [link]"
    assert read.json()["body_format"] == "plain_text"
    assert read.json()["has_attachments"] is True
    assert read.json()["attachment_count"] == 1
    assert "PRIVATE ATTACHMENT CONTENT" not in read.text
    assert set(methods) == {"GET"}


async def test_gmail_filter_only_dates_use_the_requested_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    seen_queries: list[str | None] = []

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/users/me/messages"):
            seen_queries.append(request.url.params.get("q"))
            return Response(200, json={"messages": []})
        return Response(404)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            by_label = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "", "label_ids": ["INBOX"]},
            )
            by_date = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={
                    "query": "",
                    "since_date": "2026-07-09",
                    "until_date": "2026-07-09",
                    "time_zone": "America/New_York",
                },
            )
            unbounded = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": ""},
            )
            blank_labels = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "", "label_ids": [" ", "\t"]},
            )

    assert by_label.status_code == 200
    assert by_date.status_code == 200
    assert unbounded.status_code == 422
    assert blank_labels.status_code == 422
    assert seen_queries == [None, "after:1783569600 before:1783656000"]


def test_gmail_openapi_and_recipient_contract(tmp_path):
    schemas = make_app(_config(tmp_path)).openapi()["components"]["schemas"]
    assert "page_token" in schemas["GmailSearchRequest"]["properties"]
    assert "next_page_token" in schemas["GmailSearchResponse"]["properties"]
    assert "time_zone" in schemas["GmailSearchRequest"]["properties"]
    assert "body_format" in schemas["GmailReadResponse"]["properties"]
    assert "attachment_count" in schemas["GmailReadResponse"]["properties"]
    # Marks a capped search hit, so it is distinguishable from a short list (#140).
    assert "recipients_omitted" in schemas["GmailMessageMetadata"]["properties"]
    assert _recipient_headers(
        {
            "to": '"Doe, Jane" <jane@example.com>, John <john@example.com>',
            "cc": "Team <team@example.com>",
        }
    ) == [
        '"Doe, Jane" <jane@example.com>',
        "John <john@example.com>",
        "Team <team@example.com>",
    ]


def test_gmail_content_info_distinguishes_multipart_bodies_and_attachments():
    assert _message_content_info(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"data": "plain"}},
                {"mimeType": "text/html", "filename": "", "body": {"data": "html"}},
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"attachmentId": "attachment-1"},
                },
            ],
        }
    ) == ("multipart", 1)


def test_gmail_decode_body_returns_empty_text_for_malformed_base64():
    # One data character is not valid base64 even once padding is added.
    assert _decode_body("a") == ""
    assert _decode_body("aGVsbG8") == "hello"


# A fake mailbox shared by the payload, recipient, label and snippet tests below.

SNIPPET = "Reminder: the quarterly all-hands is moved to Thursday at 10am."
LABELS = ["INBOX", "UNREAD", "IMPORTANT", "CATEGORY_UPDATES", "Label_42"]

# Every setting, with its default. A case lists only what it changes.
BASE = dict(
    # Fake mailbox (what "Gmail" returns)
    to_count=150,
    cc_count=0,
    labels=LABELS,
    snippet=SNIPPET,
    subject="Q3 all-hands",
    body="The all-hands moves to Thursday. " * 100,
    html_only=False,
    # When set, the message is multipart/alternative: ``body`` is the plain-text
    # part (which the read prefers) and this is the HTML part.
    html_body=None,
    # Request (what the caller asks the Hub for)
    limit=25,
    label_filter=[],  # search request label_ids
    max_chars=12_000,
)

# Payload-size regression cases for #140. Each is a settings override on BASE
# plus the largest search response, in bytes, it may produce.
CASES = {
    # The issue's scenario: a full 25-hit page (the maximum limit), every hit a
    # company-wide email to 150 people in To, with 5 labels. Before the fix the
    # search was 182,234 B, 96% of it recipients; with the 10-recipient preview
    # it is ~19 KB. Fails if the cap is removed or raised to 15 or more
    # (each extra recipient adds ~1.2 KB across the page).
    "worst": ({}, 25_000),
    # An ordinary search: 10 hits (Apollo's default limit), each to 3 people, so
    # no list is cut. Before the fix the search was 4,614 B; complete lists carry
    # no recipients_omitted, so it is sent exactly as before and must never grow
    # past that. Fails if any hit grows at all, e.g. a count added to every hit.
    "typical": ({"to_count": 3, "limit": 10}, 4_614),
}


def _addresses(prefix: str, count: int) -> str:
    return ", ".join(f"{prefix.title()} {i} <{prefix}{i}@example.com>" for i in range(count))


def _fake_gmail(p: dict):
    headers = [
        {"name": "Subject", "value": p["subject"]},
        {"name": "From", "value": "CEO <ceo@example.com>"},
    ]
    if p["to_count"]:
        headers.append({"name": "To", "value": _addresses("person", p["to_count"])})
    if p["cc_count"]:
        headers.append({"name": "Cc", "value": _addresses("cc", p["cc_count"])})

    if p["html_body"] is None:
        payload = {
            "mimeType": "text/html" if p["html_only"] else "text/plain",
            "headers": headers,
            "body": {"data": _encoded(p["body"])},
        }
    else:
        payload = {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"data": _encoded(p["body"])}},
                {"mimeType": "text/html", "filename": "", "body": {"data": _encoded(p["html_body"])}},
            ],
        }

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/users/me/messages"):
            ids = [{"id": f"m{i}"} for i in range(p["limit"])]
            return Response(200, json={"messages": ids, "resultSizeEstimate": len(ids)})
        return Response(
            200,
            json={
                "id": request.url.path.rsplit("/", 1)[-1],
                "threadId": "t1",
                "snippet": p["snippet"],
                "labelIds": p["labels"],
                "internalDate": "1790000000000",
                "payload": payload,
            },
        )

    return handler


async def _search_and_read(p: dict, tmp_path, monkeypatch) -> tuple[Response, Response]:
    """Run one search and one read of ``m0`` against the fake mailbox described by ``p``."""
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    _install_mock_client(monkeypatch, _fake_gmail(p))
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            search = await client.post(
                "/v1/connectors/gmail/search",
                headers=_auth_header(),
                json={"query": "all-hands", "limit": p["limit"], "label_ids": p["label_filter"]},
            )
            read = await client.post(
                "/v1/connectors/gmail/messages/m0/read",
                headers=_auth_header(),
                json={"max_chars": p["max_chars"]},
            )

            return search, read

@pytest.mark.parametrize("case", CASES)
async def test_gmail_payload_breakdown(case, tmp_path, monkeypatch):
    overrides, max_search_bytes = CASES[case]
    p = {**BASE, **overrides}  # defaults, overridden by this case
    search, read = await _search_and_read(p, tmp_path, monkeypatch)

    # Printed so the test doubles as a measurement tool: run with -s to see it.
    hit = search.json()["messages"][0]
    per_hit = {field: len(json.dumps(hit[field])) for field in ("recipients", "label_ids", "snippet")}
    print(f"\n{case:>18}: search={len(search.content):>7}  read={len(read.content):>6}  per-hit={per_hit}")

    assert len(search.content) <= max_search_bytes


# Recipients each search hit may carry (#140). Kept as a literal rather than
# imported from gmail_client so this file still imports before the cap exists.
SEARCH_RECIPIENT_CAP = 10


@pytest.mark.parametrize(
    ("to_count", "cc_count"),
    [(0, 0), (3, 0), (10, 0), (11, 0), (150, 0), (5, 20)],
    ids=["none", "under-cap", "at-cap", "one-over-cap", "large-list", "to-then-cc"],
)
async def test_gmail_search_caps_recipients_and_read_keeps_them_all(to_count, cc_count, tmp_path, monkeypatch):
    p = {**BASE, "to_count": to_count, "cc_count": cc_count}
    search, read = await _search_and_read(p, tmp_path, monkeypatch)
    total = to_count + cc_count

    # The read is the full source of truth: every recipient, To before Cc, and
    # nothing omitted, so the marker never appears.
    message = read.json()["message"]
    full = message.get("recipients", [])
    assert [value.split(" ")[0] for value in full] == ["Person"] * to_count + ["Cc"] * cc_count
    assert "recipients_omitted" not in message
    # Over HTTP too, only default-valued fields are left out: the marker, and
    # the recipient list itself when there are no recipients.
    fields = set(GmailMessageMetadata.model_fields)
    empty = {"recipients_omitted"} | ({"recipients"} if total == 0 else set())
    assert set(message) == fields - empty

    # Each search hit carries only the first addresses of that list. A cut list
    # says how many were dropped; a complete one carries no marker at all.
    omitted = max(0, total - SEARCH_RECIPIENT_CAP)
    for hit in search.json()["messages"]:
        shown = hit.get("recipients", [])
        assert len(shown) == min(total, SEARCH_RECIPIENT_CAP)
        assert shown == full[:SEARCH_RECIPIENT_CAP]
        if omitted:
            assert hit["recipients_omitted"] == omitted
            assert set(hit) == fields
        else:
            assert set(hit) == fields - empty


def test_gmail_metadata_serializer_drops_only_default_valued_fields():
    # A message field at its default ("", [], None, 0) is left out (#137); one
    # set to anything else is sent, whatever its type.
    bare = GmailMessageMetadata(id="m0")
    assert bare.model_dump(mode="json") == {"id": "m0"}
    assert json.loads(bare.model_dump_json()) == {"id": "m0"}

    full = GmailMessageMetadata(
        id="m0",
        thread_id="t0",
        subject="Q3 plan",
        sender="Dana",
        recipients=["Maya"],
        recipients_omitted=5,
        sent_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        snippet="Approved.",
        label_ids=["INBOX"],
    )
    assert set(full.model_dump(mode="json")) == set(GmailMessageMetadata.model_fields)

    # Each field is judged on its own value.
    mixed = GmailMessageMetadata(id="m0", subject="Q3 plan", label_ids=[])
    assert mixed.model_dump(mode="json") == {"id": "m0", "subject": "Q3 plan"}

    # The response envelopes keep every field, even at its default: the
    # untrusted-content boundary (content_trust, security_notice), and the
    # next_page_token whose "" tells callers the search is complete.
    search = GmailSearchResponse(messages=[bare]).model_dump(mode="json")
    assert set(search) == set(GmailSearchResponse.model_fields)
    assert search["next_page_token"] == ""
    read = GmailReadResponse(message=bare, text="", truncated=False).model_dump(mode="json")
    assert set(read) == set(GmailReadResponse.model_fields)
    assert read["content_trust"] == "external_untrusted"
    assert read["message"] == {"id": "m0"}


# One set value per optional message field, each as close to empty as a real
# value gets (a space, "0", a list holding an empty string, the 1970 epoch).
# Only the exact default may be dropped, so each must survive on its own: this
# catches a rule that drops a field by name, or that "tidies" whitespace-only
# text into empty.
NON_DEFAULT_VALUES = {
    "thread_id": "0",
    "subject": " ",
    "sender": "0",
    "recipients": [""],
    "recipients_omitted": 1,
    "sent_at": datetime(1970, 1, 1, tzinfo=timezone.utc),
    "snippet": "0",
    "label_ids": [""],
}


def test_gmail_metadata_non_default_values_cover_every_optional_field():
    # Fails when a field is added to GmailMessageMetadata without a case below,
    # so no future field escapes the keep-when-set check.
    optional = {name for name, field in GmailMessageMetadata.model_fields.items() if not field.is_required()}
    assert set(NON_DEFAULT_VALUES) == optional


@pytest.mark.parametrize("field", sorted(NON_DEFAULT_VALUES))
def test_gmail_metadata_serializer_keeps_each_field_that_is_set(field):
    message = GmailMessageMetadata(id="m0", **{field: NON_DEFAULT_VALUES[field]})
    for dumped in (message.model_dump(mode="json"), json.loads(message.model_dump_json())):
        # Exactly the set field survives beside id: nothing else is added, and
        # the set field is not mistaken for an empty one.
        assert set(dumped) == {"id", field}
    if field != "sent_at":
        assert message.model_dump(mode="json")[field] == NON_DEFAULT_VALUES[field]
    else:
        assert message.model_dump(mode="json")["sent_at"] == "1970-01-01T00:00:00Z"


async def test_gmail_search_keeps_every_label(tmp_path, monkeypatch):
    # Regression guard (#140 eval). Search hits once hid IMPORTANT,
    # CATEGORY_PERSONAL and any label the request filtered on. Asked "Did Gmail
    # mark that email as important?", the model answered from the hit 3 times
    # out of 3, reading the missing label as "no", so hits now carry every
    # label Gmail sends -- including under a label filter.
    labels = ["INBOX", "UNREAD", "STARRED", "IMPORTANT", "CATEGORY_PERSONAL", "CATEGORY_PROMOTIONS", "SENT", "Label_42"]
    p = {**BASE, "labels": labels, "label_filter": ["STARRED"], "to_count": 3}
    search, read = await _search_and_read(p, tmp_path, monkeypatch)

    for hit in search.json()["messages"]:
        assert hit["label_ids"] == labels
    assert read.json()["message"]["label_ids"] == labels


# Gmail's snippet is a preview of the body's opening, so realistic fixtures
# derive it from the body rather than inventing unrelated text.
BODY = "The all-hands moves to Thursday at 10am. Bring questions for the Q&A. " * 20
REAL_SNIPPET = BODY[:200].strip()


async def test_gmail_read_drops_the_snippet_that_its_text_repeats(tmp_path, monkeypatch):
    p = {**BASE, "body": BODY, "snippet": REAL_SNIPPET, "to_count": 3}
    search, read = await _search_and_read(p, tmp_path, monkeypatch)

    # The read's text starts with everything the snippet said, so the snippet
    # is emptied rather than sent twice.
    message = read.json()["message"]
    assert read.json()["text"].startswith(REAL_SNIPPET)
    # Dropped, and like any empty message field, not sent at all.
    assert "snippet" not in message

    # Search has no body, so its snippet is the only preview and must remain.
    for hit in search.json()["messages"]:
        assert hit["snippet"] == REAL_SNIPPET


async def test_gmail_read_keeps_the_snippet_when_there_is_no_body_text(tmp_path, monkeypatch):
    # With no extractable body text (e.g. an attachment-only message) the
    # snippet is the only content, so there is nothing to deduplicate against.
    p = {**BASE, "body": "", "snippet": REAL_SNIPPET, "to_count": 3}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["text"] == ""
    assert read.json()["message"]["snippet"] == REAL_SNIPPET


async def test_gmail_read_keeps_the_snippet_when_max_chars_cuts_it_off(tmp_path, monkeypatch):
    # The returned text is only the first 20 characters, so most of what the
    # snippet says is not in the response: dropping it would lose content.
    p = {**BASE, "body": BODY, "snippet": REAL_SNIPPET, "to_count": 3, "max_chars": 20}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["truncated"] is True
    assert len(read.json()["text"]) == 20
    assert read.json()["message"]["snippet"] == REAL_SNIPPET


async def test_gmail_read_drops_the_snippet_when_truncated_text_still_contains_it(tmp_path, monkeypatch):
    # Truncation alone is not a reason to keep it: here the cut falls after the
    # snippet's last character, so the returned text still repeats all of it.
    p = {**BASE, "body": BODY, "snippet": REAL_SNIPPET, "to_count": 3, "max_chars": len(REAL_SNIPPET) + 10}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["truncated"] is True
    assert REAL_SNIPPET in read.json()["text"]
    assert "snippet" not in read.json()["message"]


async def test_gmail_read_still_sanitizes_text_after_the_snippet_is_dropped(tmp_path, monkeypatch):
    # Regression: the snippet was a sanitized field; dropping it must not be
    # the only thing standing between a raw link and the model. The body text
    # carries the same content and must still be link-sanitized.
    body = "Review the plan at https://unsafe.example.test/plan before Friday."
    p = {**BASE, "body": body, "snippet": body, "to_count": 3}
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert "unsafe.example.test" not in read.text
    assert read.json()["text"] == "Review the plan at [link] before Friday."
    assert "snippet" not in read.json()["message"]


# Review of #140: Gmail builds the snippet itself, so it is not guaranteed to
# repeat the text the read returns. These cases pin down exactly when the two
# count as the same content. Everything not listed as a known formatting
# difference must keep the snippet: when in doubt, keep it.
@pytest.mark.parametrize(
    ("snippet", "text"),
    [
        ("Approved: release Friday.", "Approved: release Friday. Details below."),
        (
            "The budget is signed off. Please proceed with the ven",
            "The budget is signed off. Please proceed with the vendor.",
        ),
        ("release Friday", "Hi all, the release Friday is approved."),
        ("It&#39;s approved", "It's approved."),
        ("Q&amp;A at 5 &gt; 4 &lt; 6", "Q&A at 5 > 4 < 6 today"),
        ("&quot;Ship it&quot;, said Dana", '"Ship it", said Dana.'),
        ("Caf&eacute; &#x2014; noon", "Café — noon"),
        ("Hello team please review", "Hello team\nplease review"),
        ("Hello  team,\tplease review", "Hello team, please review"),
        ("Hello\u00a0team", "Hello team"),
        ("Sale ends\u200c today\u034f \u200b", "Sale ends today"),
        ("\ufeffMorning update", "Morning update"),
        ("Cafe\u0301 opens at noon", "Caf\u00e9 opens at noon"),
        ("See [link] before Friday", "See [link] before Friday, thanks."),
        ("\u200c \u034f ", "Anything at all"),
    ],
    ids=[
        "prefix",
        "cut-mid-word",
        "inside-text",
        "numeric-entity",
        "named-entities",
        "quot-entity",
        "accent-and-dash-entities",
        "newline-in-text",
        "extra-spaces-and-tab",
        "non-breaking-space",
        "zero-width-padding",
        "byte-order-mark",
        "decomposed-accent",
        "sanitized-link",
        "invisible-only-snippet",
    ],
)
def test_gmail_snippet_counts_as_repeated_only_across_formatting_differences(snippet, text):
    assert _snippet_is_repeated(snippet, text)


@pytest.mark.parametrize(
    ("snippet", "text"),
    [
        ("Approved: release Friday.", "Please view the HTML version of this message."),
        ("Approved: release Friday.", "Approved: rel"),
        ("Approved: release Friday.", ""),
        ("APPROVED", "approved"),
        ("Release Friday", "Release on Friday"),
        ("Approved!", "Approved."),
        ("It\u2019s approved", "It's approved"),
        ("It&amp;#39;s approved", "It's approved"),
        ("Approved: release Fri\u2026", "Approved: release Friday"),
        ("a &lt; b", "a &lt; b"),
        ("Ship it on Friday", "Ship it\nThen on Friday"),
        ("Contact maya@exam", "Contact maya [at] example [dot] com"),
        ("Total 1,000", "Total 1000"),
    ],
    ids=[
        "different-part",
        "cut-by-max-chars",
        "no-text",
        "case-differs",
        "wording-differs",
        "punctuation-differs",
        "curly-vs-straight-quote",
        "double-escaped-entity",
        "ellipsis-added",
        "entity-literal-in-text",
        "words-in-different-order",
        "address-cut-before-sanitizing",
        "number-formatting-differs",
    ],
)
def test_gmail_snippet_is_kept_whenever_the_text_does_not_carry_it(snippet, text):
    assert not _snippet_is_repeated(snippet, text)


async def test_gmail_read_keeps_a_snippet_from_the_html_part_behind_a_plain_text_stub(tmp_path, monkeypatch):
    # The reviewer's case: the read selects the plain-text part, which is only a
    # stub, while Gmail built the snippet from the HTML part. The snippet is the
    # only place the approval appears, so it must survive the read.
    p = {
        **BASE,
        "to_count": 3,
        "body": "Please view the HTML version of this message.",
        "html_body": "<p>Approved: release Friday.</p><p>Thanks, Dana</p>",
        "snippet": "Approved: release Friday. Thanks, Dana",
    }
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    body = read.json()
    assert body["text"] == "Please view the HTML version of this message."
    assert body["truncated"] is False
    assert body["body_format"] == "multipart"
    assert body["message"]["snippet"] == "Approved: release Friday. Thanks, Dana"


async def test_gmail_read_drops_a_multipart_snippet_that_the_plain_text_repeats(tmp_path, monkeypatch):
    # The ordinary multipart case: both parts say the same thing, so the
    # selected plain text already carries the snippet.
    p = {
        **BASE,
        "to_count": 3,
        "body": "Approved: release Friday.\n\nThanks, Dana",
        "html_body": "<p>Approved: release Friday.</p><p>Thanks, Dana</p>",
        "snippet": "Approved: release Friday. Thanks, Dana",
    }
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["body_format"] == "multipart"
    assert "snippet" not in read.json()["message"]


async def test_gmail_read_drops_an_html_escaped_snippet_that_the_text_repeats(tmp_path, monkeypatch):
    # Gmail escapes characters in snippets; the body carries them unescaped.
    # They are the same content, so the snippet goes.
    p = {
        **BASE,
        "to_count": 3,
        "body": "It's approved: Q&A moves to 5 > 4 o'clock.",
        "snippet": "It&#39;s approved: Q&amp;A moves to 5 &gt; 4 o&#39;clock.",
    }
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert "snippet" not in read.json()["message"]


async def test_gmail_read_drops_the_snippet_of_an_html_only_message_it_repeats(tmp_path, monkeypatch):
    # HTML-only mail: the read converts the HTML to text (paragraphs become
    # line breaks, entities are decoded), and the snippet still matches it.
    p = {
        **BASE,
        "to_count": 3,
        "html_only": True,
        "body": "<p>It&#39;s approved.</p><p>Release&nbsp;Friday.</p>",
        "snippet": "It&#39;s approved. Release Friday.",
    }
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    # Paragraphs become a blank line, and the non-breaking space an ordinary
    # space; the snippet still matches because whitespace is forgiven.
    assert read.json()["text"] == "It's approved.\n\nRelease Friday."
    assert "snippet" not in read.json()["message"]


async def test_gmail_read_keeps_a_snippet_that_differs_from_the_text(tmp_path, monkeypatch):
    # Any real difference in wording counts as new information: keep it.
    p = {
        **BASE,
        "to_count": 3,
        "body": "The release is approved.",
        "snippet": "The release is approved for Friday.",
    }
    _search, read = await _search_and_read(p, tmp_path, monkeypatch)

    assert read.json()["message"]["snippet"] == "The release is approved for Friday."
