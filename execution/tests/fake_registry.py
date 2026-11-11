"""A stdlib OCI distribution registry for the tests: blobs and manifests by digest, tags, and the token flow.

Enough of the distribution API for ``artifacts.publish`` to push a package and
``artifacts.materialize`` to pull it back: monolithic blob uploads, manifests
by tag or digest, and, when ``credential`` is given, a ``Bearer`` challenge
on every request that a token from ``/token`` answers, as the hub's own
``/v2/`` and a private registry do.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeRegistry:
    def __init__(self, *, credential: str | None = None) -> None:
        self.credential = credential
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], bytes] = {}  # (repository, digest) -> manifest
        self.tags: dict[tuple[str, str], str] = {}  # (repository, tag) -> digest
        self.uploads: dict[str, str] = {}
        self.tokens: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.blob_reads: list[str] = []
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _authorized(self) -> bool:
                if registry.credential is None:
                    return True
                presented = self.headers.get("Authorization", "")
                if presented.startswith("Bearer ") and presented[7:] in registry.tokens:
                    return True
                self._send(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}', {
                    "WWW-Authenticate": f'Bearer realm="http://{self.headers.get("Host")}/token",'
                                        f'service="{self.headers.get("Host")}",scope="repository:x:pull"'})
                return False

            def _token(self) -> None:
                presented = self.headers.get("Authorization", "")
                ok = False
                if presented.startswith("Basic "):
                    ok = base64.b64decode(presented[6:]).decode() == registry.credential
                if not ok:
                    self._send(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}')
                    return
                token = secrets.token_urlsafe(16)
                registry.tokens.add(token)
                self._send(200, json.dumps({"token": token}).encode(), {"Content-Type": "application/json"})

            def do_GET(self) -> None:  # noqa: N802
                self._handle()

            def do_HEAD(self) -> None:  # noqa: N802
                self._handle()

            def do_POST(self) -> None:  # noqa: N802
                self._handle()

            def do_PUT(self) -> None:  # noqa: N802
                self._handle()

            def _handle(self) -> None:
                path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
                registry.requests.append((self.command, path))
                if path == "/token":
                    return self._token()
                if not self._authorized():
                    return
                if path == "/v2/":
                    return self._send(200, b"{}")
                match = re.match(r"^/v2/(?P<repo>.+)/(?P<kind>manifests|blobs)/(?P<ref>[^/]+)$", path)
                if match and match["kind"] == "manifests":
                    return self._manifest(match["repo"], match["ref"])
                if match and match["kind"] == "blobs" and match["ref"] != "uploads":
                    return self._blob(match["repo"], match["ref"])
                upload = re.match(r"^/v2/(?P<repo>.+)/blobs/uploads/(?P<id>[^/]*)$", path)
                if upload:
                    return self._upload(upload["repo"], upload["id"], query)
                self._send(404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')

            def _manifest(self, repo: str, ref: str) -> None:
                if self.command == "PUT":
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
                    registry.manifests[(repo, digest)] = body
                    if not ref.startswith("sha256:"):
                        registry.tags[(repo, ref)] = digest
                    return self._send(201, b"", {"Docker-Content-Digest": digest, "Location": self.path})
                digest = ref if ref.startswith("sha256:") else registry.tags.get((repo, ref))
                body = registry.manifests.get((repo, digest or ""))
                if body is None:
                    return self._send(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                self._send(200, body, {"Content-Type": "application/vnd.oci.image.manifest.v1+json",
                                       "Docker-Content-Digest": digest})

            def _blob(self, repo: str, digest: str) -> None:
                body = registry.blobs.get((repo, digest))
                if body is None:
                    return self._send(404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}')
                if self.command == "GET":
                    registry.blob_reads.append(digest)
                self._send(200, body, {"Content-Type": "application/octet-stream", "Docker-Content-Digest": digest})

            def _upload(self, repo: str, upload_id: str, query: dict) -> None:
                if self.command == "POST":
                    upload_id = secrets.token_hex(8)
                    registry.uploads[upload_id] = repo
                    return self._send(202, b"", {"Location": f"/v2/{repo}/blobs/uploads/{upload_id}"})
                if self.command == "PUT" and upload_id in registry.uploads:
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
                    if query.get("digest", [None])[0] != digest:
                        return self._send(400, b'{"errors":[{"code":"DIGEST_INVALID"}]}')
                    registry.blobs[(repo, digest)] = body
                    del registry.uploads[upload_id]
                    return self._send(201, b"", {"Docker-Content-Digest": digest})
                self._send(404, b"")

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> FakeRegistry:
        self.thread.start()
        return self

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def corrupt(self, digest: str) -> None:
        """Replace the stored bytes of a blob, so what is served no longer matches its digest."""
        for key in list(self.blobs):
            if key[1] == digest:
                self.blobs[key] = self.blobs[key] + b"tampered"
