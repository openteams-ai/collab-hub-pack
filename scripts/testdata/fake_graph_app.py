"""Standalone fake Microsoft Graph for local SharePoint connector smoke tests (stdlib only).

Serves just enough of the read-only Graph surface the Collab Hub connector uses:

    GET  /v1.0/me
    POST /v1.0/search/query                          (two offset pages)
    GET  /v1.0/drives/{drive}/items/{item}
    GET  /v1.0/drives/{drive}/items/{item}/content   (302 to a download URL, like Graph)
    GET  /v1.0/drives/{drive}/items/{item}/content?format=pdf
    GET  /download/...                               (the "other host" the 302 points at)
    GET  /broker/token                               (Keycloak-style broker stand-in)

Every resource carries ``webUrl`` and the search snippet carries ``<c0>``/``<ddd/>``
markup so smoke assertions can prove both are dropped. One hit lives in a
personal OneDrive so the default exclusion is exercised. The download host
records whether an ``Authorization`` header arrived: it must not. Bind loopback only.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

GRAPH_TOKEN = "eyJ-fake-graph-access-token"
DRIVE_ID = "b!fakeDrive-AbC123_xyz"
DOCX_ID = "01FAKEDOCX0000000000000000000001"
TXT_ID = "01FAKETXT00000000000000000000002"
PNG_ID = "01FAKEPNG00000000000000000000003"
PERSONAL_ID = "01FAKEPERSONAL000000000000000004"
SITE_URL = "https://fake.sharepoint.example/sites/Marketing/Shared%20Documents"
PERSONAL_URL = "https://fake-my.sharepoint.example/personal/alice/Documents"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_BODY = b"Q3 marketing notes\r\n\r\nAction item: Alice sends the report by Friday.\r\nSee www.evil.example\r\n"

SEEN_AUTHORIZATION_ON_DOWNLOAD: list[str | None] = []


def _resource(
    item_id: str,
    name: str,
    *,
    web_url: str,
    mime: str = "",
    size: int = 4321,
    folder: bool = False,
) -> dict:
    resource = {
        "@odata.type": "#microsoft.graph.driveItem",
        "id": item_id,
        "name": name,
        "size": size,
        "webUrl": web_url,
        "lastModifiedDateTime": "2026-10-06T10:00:00Z",
        "lastModifiedBy": {
            "user": {"displayName": "Bob Example", "email": "bob@fake.example"}
        },
        "parentReference": {
            "driveId": DRIVE_ID,
            "siteId": "fake.sharepoint.example,guid1,guid2",
        },
    }
    if folder:
        resource["folder"] = {"childCount": 1}
    else:
        resource["file"] = {"mimeType": mime}
    return resource


ITEMS = {
    DOCX_ID: _resource(
        DOCX_ID,
        "Q3 Marketing Plan.docx",
        web_url=f"{SITE_URL}/Q3%20Marketing%20Plan.docx",
        mime=DOCX_MIME,
    ),
    TXT_ID: _resource(
        TXT_ID,
        "notes.txt",
        web_url=f"{SITE_URL}/notes.txt",
        mime="text/plain",
        size=len(TXT_BODY),
    ),
    PNG_ID: _resource(
        PNG_ID, "logo.png", web_url=f"{SITE_URL}/logo.png", mime="image/png"
    ),
    PERSONAL_ID: _resource(
        PERSONAL_ID,
        "My draft.docx",
        web_url=f"{PERSONAL_URL}/My%20draft.docx",
        mime=DOCX_MIME,
    ),
}


def _hit(item_id: str, summary: str) -> dict:
    return {"hitId": item_id, "rank": 1, "summary": summary, "resource": ITEMS[item_id]}


PAGE_ONE = [
    _hit(DOCX_ID, "<c0>Marketing</c0> plan for Q3<ddd/>"),
    _hit(PERSONAL_ID, "private <c0>marketing</c0> draft"),
]
PAGE_TWO = [_hit(TXT_ID, "Q3 marketing notes"), _hit(PNG_ID, "")]


def minimal_pdf(text: str) -> bytes:
    """A hand-built one-page PDF with ``text`` in Helvetica, enough for PDFium."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 24 Tf 72 700 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


CONVERTED_DOCX_PDF = minimal_pdf("Hello SharePoint from the fake converter")


class Handler(BaseHTTPRequestHandler):
    base_url = "http://127.0.0.1:8976"

    def log_message(self, *args) -> None:
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _graph_error(self, status: int, message: str) -> None:
        self._send_json(
            status,
            {
                "error": {
                    "code": "fakeCode",
                    "message": message,
                    "innerError": {"request-id": "SECRET-REQ-ID"},
                }
            },
        )

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def _require_token(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {GRAPH_TOKEN}":
            self._graph_error(401, "InvalidAuthenticationToken")
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        # Drive ids carry '!' which arrives percent-encoded (%21) over a real socket.
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path == "/broker/token":
            self._send_json(
                200,
                {
                    "access_token": GRAPH_TOKEN,
                    "refresh_token": "fake-refresh",
                    "expires_in": 3599,
                },
            )
            return
        if path.startswith("/download/"):
            # The "other host": Graph's 302 lands here WITHOUT the Graph token.
            SEEN_AUTHORIZATION_ON_DOWNLOAD.append(self.headers.get("Authorization"))
            if path.endswith("/converted"):
                self._send_bytes(CONVERTED_DOCX_PDF, "application/pdf")
            elif path.endswith("/notes.txt"):
                self._send_bytes(TXT_BODY, "text/plain")
            else:
                self._send_bytes(b"\x89PNG\r\n\x1a\n", "image/png")
            return
        if not self._require_token():
            return
        if path == "/v1.0/me":
            self._send_json(
                200,
                {
                    "displayName": "Fake User",
                    "userPrincipalName": "fake@fake.onmicrosoft.example",
                },
            )
            return
        if path.startswith(f"/v1.0/drives/{DRIVE_ID}/items/"):
            remainder = path[len(f"/v1.0/drives/{DRIVE_ID}/items/") :]
            item_id, _, tail = remainder.partition("/")
            item = ITEMS.get(item_id)
            if item is None:
                self._graph_error(404, "The resource could not be found.")
                return
            if tail == "":
                self._send_json(200, item)
                return
            if tail == "content":
                if query.get("format") == ["pdf"]:
                    if item["name"].endswith(".docx"):
                        self._redirect(f"{self.base_url}/download/converted")
                    else:
                        self._graph_error(415, "InputFormatNotSupported")
                    return
                self._redirect(
                    f"{self.base_url}/download/{item['name'].replace(' ', '_')}"
                )
                return
        self._graph_error(404, "The resource could not be found.")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._require_token():
            return
        body = self._body()
        if path == "/v1.0/search/query":
            request = (body.get("requests") or [{}])[0]
            offset = int(request.get("from") or 0)
            hits = PAGE_ONE if offset == 0 else PAGE_TWO if offset < 4 else []
            self._send_json(
                200,
                {
                    "value": [
                        {
                            "searchTerms": [
                                request.get("query", {}).get("queryString", "")
                            ],
                            "hitsContainers": [
                                {
                                    "hits": hits,
                                    "total": 4,
                                    "moreResultsAvailable": offset + len(hits) < 4,
                                }
                            ],
                        }
                    ]
                },
            )
            return
        self._graph_error(404, "The resource could not be found.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fake Microsoft Graph for SharePoint smoke tests"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8976)
    parser.add_argument(
        "--public-url",
        default="",
        help=(
            "Origin the Hub reaches this fake at, used in /content redirects "
            "(e.g. http://127.0.0.1:8084 under dev/compose.yaml). "
            "Defaults to http://<host>:<port>."
        ),
    )
    args = parser.parse_args()
    Handler.base_url = args.public_url.rstrip("/") or f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"fake graph listening on http://{args.host}:{args.port} (public {Handler.base_url})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
