"""Cog artifacts: a published package pulled by its pinned reference, and the cache its environment lives in.

A published Cog is an OCI artifact, ``<host>/<repository>@sha256:<digest>``,
whose layers are the package's files, each named by its
``org.opencontainers.image.title`` annotation: ``pixi.toml``, ``pixi.lock``,
``COG.md`` and whatever else the bundle carries. This module pulls one
through any OCI distribution endpoint, the hub's own ``/v2/`` included, and
is what a ``remote`` worker's init container runs:

    python -m collab_hub_execution.artifacts materialize <reference> --cache /var/cache/collab-hub
    python -m collab_hub_execution.artifacts serve <reference> --cache /var/cache/collab-hub

``materialize`` pulls the artifact into the cache, under a directory named by
the artifact's own digest (``packages/<digest>``), and verifies every blob
against its digest; a package found there is verified file for file against
the manifest before it is trusted, and pulled again otherwise. ``serve``
finds that directory and runs the package's ``serve`` task there with pixi,
``--locked``. The environment is the lock's: ``envs/<lock digest>``, which
the package's ``.pixi`` links to, so two artifacts with one lock share one
environment and a second run on the same lock solves nothing again, while
no two artifacts ever share a package. ``evict`` removes a package, and the
environment of its lock unless another install shares it.

Authentication is the distribution token flow: a ``401`` with a ``Bearer``
challenge is answered by asking the challenge's realm for a token, with a
registry credential (``COLLAB_REGISTRY_CREDENTIAL``, ``user:secret``: what
the hub exchanges a session for at ``POST /v1/cogs/registry-credentials``)
or a bearer the registry already accepts (``COLLAB_REGISTRY_TOKEN``). A
registry reached over plain HTTP (a cluster-local one in development) is
named in ``COLLAB_REGISTRY_PLAIN_HTTP``. ``publish`` pushes a directory as
such an artifact, for development and tests; ``nebi publish`` produces the
same artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

TITLE = "org.opencontainers.image.title"
MANIFEST, LOCK, CARD = "pixi.toml", "pixi.lock", "COG.md"
SERVE_TASK = "serve"
MEDIA_TYPE_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_TYPE_PIXI_CONFIG = "application/vnd.pixi.config.v1+toml"
MEDIA_TYPE_PIXI_TOML = "application/vnd.pixi.toml.v1+toml"
MEDIA_TYPE_PIXI_LOCK = "application/vnd.pixi.lock.v1+yaml"
MEDIA_TYPE_NEBI_ASSET = "application/vnd.nebi.asset.v1"
MANIFEST_ACCEPT = ", ".join((MEDIA_TYPE_OCI_MANIFEST, MEDIA_TYPE_OCI_INDEX,
                             "application/vnd.docker.distribution.manifest.v2+json"))
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_SMALL_FILE_BYTES = 256 * 1024
"""The most a manifest, a card or a ``pixi.toml`` may be: what the install's fetch reads."""
DEFAULT_MAX_LAYER_BYTES = 4 * 1024 ** 3
"""The most one layer may be when the whole artifact is pulled (a lock, an asset)."""
MARKER = ".collab-hub-artifact"
"""The file a materialized package carries: which artifact it is, and the digest of its lock."""
CREDENTIAL_ENV, TOKEN_ENV, PLAIN_HTTP_ENV = (  # noqa: S105 - the variables' names, not secrets
    "COLLAB_REGISTRY_CREDENTIAL", "COLLAB_REGISTRY_TOKEN", "COLLAB_REGISTRY_PLAIN_HTTP")
CACHE_ENV = "COLLAB_ARTIFACT_CACHE"
EXCLUDED_FROM_PUBLISH = (".pixi", "__pycache__", ".git", MARKER)

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST = rf"(?:{_LABEL}(?:\.{_LABEL})*|\[[0-9a-fA-F:.]+\])(?::[0-9]{{1,5}})?"
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REPOSITORY = rf"{_COMPONENT}(?:/{_COMPONENT})*"
_DIGEST = r"sha256:[a-f0-9]{64}"
REFERENCE = re.compile(rf"^(?P<host>{_HOST})/(?P<repository>{_REPOSITORY})@(?P<digest>{_DIGEST})$")
TAGGED = re.compile(rf"^(?P<host>{_HOST})/(?P<repository>{_REPOSITORY}):(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{{0,127}})$")
_TITLE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_CHALLENGE_PARAM = re.compile(r'([A-Za-z0-9._~+/-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]*))')


class PullError(RuntimeError):
    """An artifact that could not be pulled: not found, refused, not a package, or not what its digest says."""


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """A pinned reference: the host to pull from, the repository, and the digest that is the Cog's identity."""

    host: str
    repository: str
    digest: str

    @classmethod
    def parse(cls, value: str) -> ArtifactReference:
        match = REFERENCE.match(value) if isinstance(value, str) else None
        if match is None:
            raise PullError(f"{value!r} is not a pinned artifact reference: <host>/<repository>@sha256:<digest>")
        return cls(match["host"], match["repository"], match["digest"])

    def __str__(self) -> str:
        return f"{self.host}/{self.repository}@{self.digest}"

    @property
    def short(self) -> str:
        return f"{self.repository}@{self.digest[:19]}"


def is_reference(value: object) -> bool:
    """Whether a step's ``cog`` names a published artifact rather than a package of the directory source."""
    return isinstance(value, str) and REFERENCE.match(value) is not None


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _hosts(value: str | None) -> frozenset[str]:
    return frozenset(host.strip() for host in (value or "").split(",") if host.strip())


class RegistryClient:
    """A synchronous OCI distribution client: manifests and blobs by digest, with the bearer token flow.

    ``credential`` is ``user:secret``, presented to the challenge's realm as
    Basic auth; ``token`` is a bearer the registry takes as it is. Hosts in
    ``plain_http`` are reached over ``http://``; every other over ``https://``.
    """

    def __init__(self, *, credential: str | None = None, token: str | None = None,
                 plain_http: Iterable[str] = (), http: httpx.Client | None = None, timeout: float = 60.0,
                 max_layer_bytes: int = DEFAULT_MAX_LAYER_BYTES) -> None:
        self.credential = credential
        self.token = token
        self.plain_http = frozenset(plain_http)
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False)
        self.max_layer_bytes = max_layer_bytes
        self._tokens: dict[tuple[str, str], str] = {}

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, **settings: Any) -> RegistryClient:
        environ = os.environ if environ is None else environ
        return cls(credential=environ.get(CREDENTIAL_ENV) or None, token=environ.get(TOKEN_ENV) or None,
                   plain_http=_hosts(environ.get(PLAIN_HTTP_ENV)), **settings)

    def base_url(self, host: str) -> str:
        scheme = "http" if host in self.plain_http else "https"
        return f"{scheme}://{host}"

    # --- requests, with the token flow --------------------------------------------------------

    def _send(self, method: str, host: str, path: str, *, headers: Mapping[str, str] | None = None,
              scope: str = "", content: bytes | None = None, stream: bool = False) -> httpx.Response:
        url = f"{self.base_url(host)}{path}"
        sent = dict(headers or {})
        cached = self._tokens.get((host, scope))
        if cached:
            sent["Authorization"] = cached
        elif self.token:
            sent["Authorization"] = f"Bearer {self.token}"
        response = self._request(method, url, sent, content, stream)
        if response.status_code != 401:
            return response
        challenge = _bearer_challenge(response.headers.get("www-authenticate", ""))
        response.close()
        if challenge is None:
            raise PullError(f"{host} refused the request and offered no bearer challenge (HTTP 401)")
        authorization = self._token_for(host, challenge, scope)
        self._tokens[(host, scope)] = authorization
        sent["Authorization"] = authorization
        response = self._request(method, url, sent, content, stream)
        if response.status_code == 401:
            response.close()
            raise PullError(f"{host} refused the token its realm issued (HTTP 401): the credential may not reach "
                            f"{scope or 'this registry'}")
        return response

    def _request(self, method: str, url: str, headers: dict[str, str], content: bytes | None,
                 stream: bool) -> httpx.Response:
        try:
            if stream:
                request = self.http.build_request(method, url, headers=headers, content=content)
                return self.http.send(request, stream=True)
            return self.http.request(method, url, headers=headers, content=content)
        except httpx.HTTPError as exc:
            raise PullError(f"cannot reach {url}: {type(exc).__name__}: {exc}") from exc

    def _token_for(self, host: str, challenge: Mapping[str, str], scope: str) -> str:
        realm = challenge.get("realm")
        if not realm:
            raise PullError(f"{host}'s bearer challenge names no realm")
        realm_host = urllib.parse.urlsplit(realm).netloc
        if not realm.startswith("https://") and realm_host not in self.plain_http:
            # A challenge is the registry's to write: the credential goes to no realm that is not https, unless
            # its host is one named as reached over plain HTTP (a cluster-local registry in development).
            raise PullError(f"{host}'s bearer challenge names the realm {realm}, which is not https and whose "
                            f"host is not in {PLAIN_HTTP_ENV}: the credential is not presented there")
        params = {key: value for key, value in (("service", challenge.get("service")),
                                                ("scope", challenge.get("scope") or scope)) if value}
        headers: dict[str, str] = {}
        auth = None
        if self.credential:
            user, _, secret = self.credential.partition(":")
            auth = (user, secret)
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.http.get(realm, params=params, headers=headers, auth=auth)
        except httpx.HTTPError as exc:
            raise PullError(f"cannot reach the token endpoint {realm}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise PullError(f"the token endpoint {realm} refused the credential (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError:
            raise PullError(f"the token endpoint {realm} answered no JSON") from None
        token = body.get("token") or body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise PullError(f"the token endpoint {realm} answered no token")
        return f"Bearer {token}"

    # --- reading -------------------------------------------------------------------------------

    def manifest(self, reference: ArtifactReference) -> dict[str, Any]:
        """The artifact's manifest, verified against the reference's digest; an index is refused."""
        response = self._send("GET", reference.host, f"/v2/{reference.repository}/manifests/{reference.digest}",
                              headers={"Accept": MANIFEST_ACCEPT}, scope=f"repository:{reference.repository}:pull")
        if response.status_code == 404:
            raise PullError(f"{reference} is not in the registry (HTTP 404)")
        if response.status_code != 200:
            raise PullError(f"{reference.host} answered HTTP {response.status_code} for the manifest of {reference}")
        body = response.content
        if len(body) > MAX_MANIFEST_BYTES:
            raise PullError(f"the manifest of {reference} is larger than {MAX_MANIFEST_BYTES} bytes")
        if _sha256(body) != reference.digest:
            raise PullError(f"the manifest served for {reference} does not match its digest")
        try:
            manifest = json.loads(body)
        except ValueError as exc:
            raise PullError(f"the manifest of {reference} is not JSON: {exc}") from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("layers"), list):
            if isinstance(manifest, dict) and isinstance(manifest.get("manifests"), list):
                raise PullError(f"{reference} is an image index, not a Cog artifact: pin one of its manifests")
            raise PullError(f"the manifest of {reference} has no layers")
        return manifest

    def blob(self, reference: ArtifactReference, digest: str, *, max_bytes: int | None = None) -> bytes:
        """One blob, verified against its digest, bounded by ``max_bytes``."""
        limit = self.max_layer_bytes if max_bytes is None else max_bytes
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        for chunk in self._stream_blob(reference, digest):
            size += len(chunk)
            if size > limit:
                raise PullError(f"the blob {digest[:19]} of {reference.short} is larger than {limit} bytes")
            hasher.update(chunk)
            chunks.append(chunk)
        if f"sha256:{hasher.hexdigest()}" != digest:
            raise PullError(f"the blob {digest[:19]} of {reference.short} does not match its digest")
        return b"".join(chunks)

    def blob_to(self, reference: ArtifactReference, digest: str, path: Path) -> int:
        """One blob written to ``path`` as it streams, verified before the file is kept; its size."""
        limit = self.max_layer_bytes
        hasher = hashlib.sha256()
        size = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        try:
            with open(partial, "wb") as handle:
                for chunk in self._stream_blob(reference, digest):
                    size += len(chunk)
                    if size > limit:
                        raise PullError(f"the blob {digest[:19]} of {reference.short} is larger than {limit} bytes")
                    hasher.update(chunk)
                    handle.write(chunk)
            if f"sha256:{hasher.hexdigest()}" != digest:
                raise PullError(f"the blob {digest[:19]} of {reference.short} does not match its digest")
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
        return size

    def _stream_blob(self, reference: ArtifactReference, digest: str):
        response = self._send("GET", reference.host, f"/v2/{reference.repository}/blobs/{digest}",
                              scope=f"repository:{reference.repository}:pull", stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise PullError(f"{reference.host} redirected the blob {digest[:19]} nowhere")
                # A registry that keeps blobs elsewhere (object storage) answers with a signed URL: followed
                # once, without the registry's credential, which is for the registry alone.
                try:
                    response = self.http.send(self.http.build_request("GET", location), stream=True)
                except httpx.HTTPError as exc:
                    raise PullError(f"cannot reach the blob's location: {type(exc).__name__}") from exc
            if response.status_code != 200:
                raise PullError(f"{reference.host} answered HTTP {response.status_code} for the blob {digest[:19]} "
                                f"of {reference.short}")
            yield from response.iter_bytes(chunk_size=256 * 1024)
        finally:
            response.close()

    # --- writing, for development and tests ---------------------------------------------------

    def push_blob(self, host: str, repository: str, data: bytes) -> str:
        """Upload one blob (monolithic), returning its digest."""
        digest = _sha256(data)
        scope = f"repository:{repository}:pull,push"
        head = self._send("HEAD", host, f"/v2/{repository}/blobs/{digest}", scope=scope)
        if head.status_code == 200:
            return digest
        started = self._send("POST", host, f"/v2/{repository}/blobs/uploads/", scope=scope)
        if started.status_code != 202:
            raise PullError(f"{host} refused to open an upload to {repository} (HTTP {started.status_code})")
        location = started.headers.get("location") or ""
        if not location:
            raise PullError(f"{host} opened an upload without a location")
        if location.startswith("/"):
            path = location
        else:
            path = location[len(self.base_url(host)):] if location.startswith(self.base_url(host)) else location
        separator = "&" if "?" in path else "?"
        finished = self._send("PUT", host, f"{path}{separator}digest={digest}", scope=scope, content=data,
                              headers={"Content-Type": "application/octet-stream",
                                       "Content-Length": str(len(data))})
        if finished.status_code not in (201, 202):
            raise PullError(f"{host} refused the blob {digest[:19]} (HTTP {finished.status_code}): "
                            f"{finished.text[:200]}")
        return digest

    def push_manifest(self, host: str, repository: str, tag: str, manifest: bytes) -> str:
        scope = f"repository:{repository}:pull,push"
        response = self._send("PUT", host, f"/v2/{repository}/manifests/{tag}", scope=scope, content=manifest,
                              headers={"Content-Type": MEDIA_TYPE_OCI_MANIFEST})
        if response.status_code not in (201, 202):
            raise PullError(f"{host} refused the manifest of {repository}:{tag} (HTTP {response.status_code}): "
                            f"{response.text[:300]}")
        return _sha256(manifest)


def _bearer_challenge(header: str) -> dict[str, str] | None:
    """The parameters of a ``WWW-Authenticate: Bearer ...`` header, or ``None`` when it is not one."""
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    params = {}
    for match in _CHALLENGE_PARAM.finditer(rest):
        key, quoted, bare = match.groups()
        params[key.lower()] = (quoted if quoted is not None else bare or "").replace('\\"', '"')
    return params


# --- the layers of an artifact -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layer:
    title: str
    digest: str
    size: int
    media_type: str


def layers_of(manifest: Mapping[str, Any], reference: ArtifactReference) -> tuple[Layer, ...]:
    """The titled layers of a manifest, each title checked to be a relative path inside the package."""
    layers = []
    for entry in manifest.get("layers", ()):
        if not isinstance(entry, dict):
            continue
        title = (entry.get("annotations") or {}).get(TITLE)
        if not isinstance(title, str):
            continue  # a layer without a title is not a file of the package
        _check_title(title, reference)
        digest, size = entry.get("digest"), entry.get("size")
        if not isinstance(digest, str) or not re.fullmatch(_DIGEST, digest):
            raise PullError(f"the layer {title!r} of {reference.short} has no sha256 digest")
        layers.append(Layer(title, digest, int(size) if isinstance(size, int) else -1,
                            str(entry.get("mediaType") or "")))
    return tuple(layers)


def _check_title(title: str, reference: ArtifactReference) -> None:
    parts = title.split("/")
    if title.startswith("/") or any(part in ("", ".", "..") or not _TITLE_SEGMENT.match(part) for part in parts):
        raise PullError(f"the layer {title!r} of {reference.short} names a path outside the package")


def lock_layer(layers: Iterable[Layer], reference: ArtifactReference) -> Layer:
    for layer in layers:
        if layer.title == LOCK:
            return layer
    raise PullError(f"{reference.short} carries no {LOCK}: a runnable package pins its environment")


def serve_task(manifest_text: bytes, reference: ArtifactReference) -> str:
    """The ``serve`` task the package's ``pixi.toml`` declares; refused when it declares none."""
    import tomllib

    try:
        document = tomllib.loads(manifest_text.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PullError(f"the {MANIFEST} of {reference.short} is not TOML: {exc}") from None
    task = document.get("tasks", {}).get(SERVE_TASK) if isinstance(document.get("tasks"), dict) else None
    command = task.get("cmd") if isinstance(task, dict) else task
    if not isinstance(command, str) or not command.strip():
        raise PullError(f"{reference.short} declares no `{SERVE_TASK}` task in its {MANIFEST}")
    return command.strip()


@dataclass(frozen=True, slots=True)
class FetchedArtifact:
    """What an install's fetch reads of an artifact: its small files, its lock's digest, and its file list."""

    reference: ArtifactReference
    lock_digest: str
    files: Mapping[str, bytes]
    """``COG.md`` and ``pixi.toml``, when the artifact carries them."""
    titles: tuple[str, ...]
    serve: str


def fetch(reference: ArtifactReference | str, client: RegistryClient) -> FetchedArtifact:
    """Read an artifact's manifest and small files without pulling it whole: what an install's fetch step does.

    Verifies the manifest against the digest, that the package carries its
    lock and declares a ``serve`` task, and reads ``COG.md`` and ``pixi.toml``.
    """
    reference = reference if isinstance(reference, ArtifactReference) else ArtifactReference.parse(reference)
    manifest = client.manifest(reference)
    layers = layers_of(manifest, reference)
    lock = lock_layer(layers, reference)
    files: dict[str, bytes] = {}
    for layer in layers:
        if layer.title in (MANIFEST, CARD):
            files[layer.title] = client.blob(reference, layer.digest, max_bytes=MAX_SMALL_FILE_BYTES)
    if MANIFEST not in files:
        raise PullError(f"{reference.short} carries no {MANIFEST}")
    return FetchedArtifact(reference=reference, lock_digest=lock.digest, files=files,
                           titles=tuple(layer.title for layer in layers), serve=serve_task(files[MANIFEST], reference))


# --- the cache -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaterializedPackage:
    """A package in the cache: where it is, which artifact it came from, the directory of its lock's environment,
    and whether it was there already."""

    reference: ArtifactReference
    directory: Path
    lock_digest: str
    serve: str
    cached: bool
    environment: Path

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST


PIXI_DIRECTORY = ".pixi"
ENVIRONMENT_PIXI = "pixi"
"""Under a lock's environment directory: what the package's ``.pixi`` links to, where pixi builds the environment."""
ENVIRONMENT_CACHE = "cache"
"""Under a lock's environment directory: pixi's package cache (``PIXI_CACHE_DIR``), the lock's own."""
ENVIRONMENT_HOME = "home"
"""Under a lock's environment directory: pixi's home (``PIXI_HOME``), the lock's own, so no ``detached-environments``
setting of the image's pixi home moves the environment away from the package's ``.pixi`` link."""


def _hex(digest: str) -> str:
    return digest.partition(":")[2]


def package_directory(cache: Path, reference: ArtifactReference) -> Path:
    """Where an artifact's files are: keyed by the artifact's own digest, so no two artifacts ever share a package,
    whatever their locks."""
    return Path(cache) / "packages" / _hex(reference.digest)


def environment_directory(cache: Path, lock_digest: str) -> Path:
    """Where the environment of a lock is: keyed by the lock's digest, shared by every artifact with that lock, since
    the lock is what names the environment."""
    return Path(cache) / "envs" / _hex(lock_digest)


def _marker(directory: Path) -> dict[str, Any] | None:
    try:
        marker = json.loads((directory / MARKER).read_text())
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def _verified(directory: Path, layers: tuple[Layer, ...]) -> str | None:
    """The ``serve`` task of the package in ``directory`` when it is the artifact these layers describe, every
    file matching its layer's digest; ``None`` when anything is missing, altered, or a link."""
    marker = _marker(directory)
    if marker is None:
        return None
    for layer in layers:
        path = directory / layer.title
        try:
            if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != layer.digest:
                return None
        except OSError:
            return None
    serve = marker.get("serve")
    return serve if isinstance(serve, str) and serve else None


def materialize(reference: ArtifactReference | str, cache: Path | str, client: RegistryClient,
                *, log=None) -> MaterializedPackage:
    """Pull the artifact into the cache under its own digest, unless its package is there and verifies.

    The package directory is keyed by the artifact's digest, so two artifacts
    never share files, whatever their locks: a new version of a Cog with the
    same lock is its own package. What they share is the environment of
    their lock, ``envs/<lock digest>``, which the package's ``.pixi`` links
    to, so pixi builds an environment once per lock and the next
    materialization of any artifact with that lock solves nothing. A package
    found in the cache is verified against the manifest, every file against
    its layer's digest, before it is trusted; one that does not verify is
    pulled again. The pull lands in a directory of its own and is moved
    into place in one step, so a package is never seen half-pulled; of two
    pulls of one artifact racing, the second finds the first's package and
    keeps it.
    """
    reference = reference if isinstance(reference, ArtifactReference) else ArtifactReference.parse(reference)
    cache = Path(cache)
    say = log or (lambda message: None)
    manifest = client.manifest(reference)
    layers = layers_of(manifest, reference)
    lock = lock_layer(layers, reference)
    directory = package_directory(cache, reference)
    environment = environment_directory(cache, lock.digest)
    serve = _verified(directory, layers)
    cached = serve is not None
    if cached:
        say(f"artifact cached: {reference.short} is {directory} (lock {lock.digest[:19]})")
    else:
        if directory.exists():
            say(f"artifact stale: {directory} is not {reference.short} file for file; pulling again")
            shutil.rmtree(directory, ignore_errors=True)
        say(f"artifact pulling: {reference.short} into {directory} (lock {lock.digest[:19]})")
        serve = _pull_into(reference, layers, directory, client, say)
    _link_environment(directory, environment)
    return MaterializedPackage(reference=reference, directory=directory, lock_digest=lock.digest, serve=serve,
                               cached=cached, environment=environment)


def _pull_into(reference: ArtifactReference, layers: tuple[Layer, ...], directory: Path, client: RegistryClient,
               say) -> str:
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".pulling-{directory.name[:12]}-", dir=directory.parent))
    try:
        for layer in layers:
            target = staging / layer.title
            size = client.blob_to(reference, layer.digest, target)
            say(f"  {layer.title} ({size} bytes)")
        manifest_file = staging / MANIFEST
        if not manifest_file.is_file():
            raise PullError(f"{reference.short} carries no {MANIFEST}")
        serve = serve_task(manifest_file.read_bytes(), reference)
        lock = lock_layer(layers, reference)
        (staging / MARKER).write_text(json.dumps({
            "reference": str(reference), "lock_digest": lock.digest, "serve": serve,
            "files": [{"title": layer.title, "digest": layer.digest} for layer in layers], "pulled_at": time.time()}))
        try:
            os.rename(staging, directory)
        except OSError:
            if _verified(directory, layers) is None:
                raise
            say("  another pull of this artifact landed first; keeping it")
        return serve
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _link_environment(directory: Path, environment: Path) -> None:
    """``<package>/.pixi`` links to the lock's environment directory: pixi builds and finds the environment there,
    once per lock, and the package stays as the artifact was pulled."""
    for name in (ENVIRONMENT_PIXI, ENVIRONMENT_CACHE, ENVIRONMENT_HOME):
        (environment / name).mkdir(parents=True, exist_ok=True)
    link = directory / PIXI_DIRECTORY
    target = environment / ENVIRONMENT_PIXI
    if link.is_symlink():
        if os.readlink(link) == str(target):
            return
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link, ignore_errors=True)
    elif link.exists():
        link.unlink()
    link.symlink_to(target, target_is_directory=True)


def locate(reference: ArtifactReference | str, cache: Path | str) -> MaterializedPackage:
    """The package ``materialize`` put in the cache for this reference; ``PullError`` when there is none."""
    reference = reference if isinstance(reference, ArtifactReference) else ArtifactReference.parse(reference)
    cache = Path(cache)
    directory = package_directory(cache, reference)
    marker = _marker(directory)
    if marker is None:
        raise PullError(f"{reference.short} has not been materialized into {cache}")
    lock_digest = str(marker.get("lock_digest") or "")
    return MaterializedPackage(reference=reference, directory=directory, lock_digest=lock_digest,
                               serve=str(marker.get("serve") or ""), cached=True,
                               environment=environment_directory(cache, lock_digest))


def evict(reference: ArtifactReference | str, cache: Path | str, *, keep_lock: bool = False) -> bool:
    """Remove the artifact's package from the cache, and the environment of its lock unless ``keep_lock`` (another
    install shares the lock). Returns whether a package was there to remove."""
    reference = reference if isinstance(reference, ArtifactReference) else ArtifactReference.parse(reference)
    cache = Path(cache)
    directory = package_directory(cache, reference)
    marker = _marker(directory)
    if marker is None:
        return False
    lock_digest = str(marker.get("lock_digest") or "")
    shutil.rmtree(directory, ignore_errors=True)
    if not keep_lock and lock_digest:
        shutil.rmtree(environment_directory(cache, lock_digest), ignore_errors=True)
    return True


def serve_command(package: MaterializedPackage, pixi: str = "pixi") -> list[str]:
    """What runs the package's ``serve`` task in its own environment: pixi, locked to the lock the digest names."""
    return [pixi, "run", "--locked", "--manifest-path", str(package.manifest), SERVE_TASK]


def serve_environment(package: MaterializedPackage) -> dict[str, str]:
    """What ``serve_command`` runs with: pixi's home and its package cache under the lock's environment directory,
    so the environment is built under the package's ``.pixi`` link and nothing of it lands outside that directory."""
    return {"PIXI_HOME": str(package.environment / ENVIRONMENT_HOME),
            "PIXI_CACHE_DIR": str(package.environment / ENVIRONMENT_CACHE)}


# --- publishing, for development and tests -------------------------------------------------------


def _media_type(title: str) -> str:
    if title == MANIFEST:
        return MEDIA_TYPE_PIXI_TOML
    if title == LOCK:
        return MEDIA_TYPE_PIXI_LOCK
    return MEDIA_TYPE_NEBI_ASSET


def package_files(directory: Path) -> list[Path]:
    """The files of a package directory to publish: everything but its environment, its caches and hidden entries.

    A path with a segment the puller would refuse as a layer title (``.git``, ``.pixi``, or the ``..data``
    and ``..<timestamp>`` entries a Kubernetes ConfigMap mount keeps beside the files it links to) is left
    out, so what is published is what can be pulled; a file reached through a symbolic link is read as it is.
    """
    found = []
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory)
        if any(part in EXCLUDED_FROM_PUBLISH or not _TITLE_SEGMENT.match(part) for part in relative.parts):
            continue
        if not path.is_file():
            continue
        found.append(path)
    return found


def publish(directory: Path | str, target: str, client: RegistryClient) -> ArtifactReference:
    """Push a package directory as a Cog artifact to ``<host>/<repository>:<tag>``; the pinned reference it got.

    The manifest is what ``nebi publish`` and ``oras push`` produce for the
    same files: one layer per file, titled by its path, the pixi media types
    for the manifest and the lock, and an empty pixi config.
    """
    match = TAGGED.match(target)
    if match is None:
        raise PullError(f"{target!r} is not <host>/<repository>:<tag>")
    host, repository, tag = match["host"], match["repository"], match["tag"]
    directory = Path(directory)
    if not (directory / MANIFEST).is_file() or not (directory / LOCK).is_file():
        raise PullError(f"{directory} is not a runnable package: it needs {MANIFEST} and {LOCK}")
    layers = []
    for path in package_files(directory):
        data = path.read_bytes()
        digest = client.push_blob(host, repository, data)
        layers.append({"mediaType": _media_type(path.relative_to(directory).as_posix()), "digest": digest,
                       "size": len(data), "annotations": {TITLE: path.relative_to(directory).as_posix()}})
    config = b"{}"
    config_digest = client.push_blob(host, repository, config)
    manifest = json.dumps({
        "schemaVersion": 2, "mediaType": MEDIA_TYPE_OCI_MANIFEST,
        "artifactType": MEDIA_TYPE_PIXI_CONFIG,
        "config": {"mediaType": MEDIA_TYPE_PIXI_CONFIG, "digest": config_digest, "size": len(config)},
        "layers": layers,
    }, separators=(",", ":")).encode()
    digest = client.push_manifest(host, repository, tag, manifest)
    return ArtifactReference(host, repository, digest)


# --- the command line: what a worker pod's containers run ----------------------------------------


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collab_hub_execution.artifacts",
                                     description="Pull a published Cog by its pinned reference, and run it.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("materialize", "pull the artifact into the cache, unless its lock's package is there"),
                            ("serve", "run the materialized package's serve task with pixi, --locked")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("reference", help="<host>/<repository>@sha256:<digest>")
        command.add_argument("--cache", default=os.environ.get(CACHE_ENV), metavar="DIR",
                             help=f"the artifact cache (or ${CACHE_ENV})")
        if name == "serve":
            command.add_argument("--pixi", default="pixi")
    gone = commands.add_parser("evict", help="remove the artifact's package from the cache")
    gone.add_argument("reference")
    gone.add_argument("--cache", default=os.environ.get(CACHE_ENV), metavar="DIR")
    gone.add_argument("--keep-lock", action="store_true",
                      help="keep the environment of its lock: another install shares it")
    pull = commands.add_parser("pull", help="pull the artifact's files into a directory")
    pull.add_argument("reference")
    pull.add_argument("--into", required=True, metavar="DIR")
    pub = commands.add_parser("publish", help="push a package directory as a Cog artifact (development)")
    pub.add_argument("directory", metavar="DIR")
    pub.add_argument("target", help="<host>/<repository>:<tag>")
    args = parser.parse_args(argv)
    try:
        client = RegistryClient.from_environment()
        if args.command in ("materialize", "serve"):
            if not args.cache:
                parser.error(f"--cache (or ${CACHE_ENV}) names the artifact cache")
            if args.command == "materialize":
                package = materialize(args.reference, args.cache, client, log=_say)
                print(str(package.directory))
                return 0
            package = locate(args.reference, args.cache)
            command = serve_command(package, args.pixi)
            _say(f"artifact serving: {package.reference.short} from {package.directory}: {' '.join(command)}")
            os.environ.update(serve_environment(package))
            os.chdir(package.directory)
            os.execvp(command[0], command)  # noqa: S606 - pixi, with the package's own serve task
            return 0
        if args.command == "evict":
            if not args.cache:
                parser.error(f"--cache (or ${CACHE_ENV}) names the artifact cache")
            removed = evict(args.reference, args.cache, keep_lock=args.keep_lock)
            _say(f"artifact {'evicted' if removed else 'not in the cache'}: {args.reference}")
            return 0
        if args.command == "pull":
            reference = ArtifactReference.parse(args.reference)
            into = Path(args.into)
            layers = layers_of(client.manifest(reference), reference)
            for layer in layers:
                client.blob_to(reference, layer.digest, into / layer.title)
                _say(f"  {layer.title}")
            return 0
        reference = publish(args.directory, args.target, client)
        print(str(reference))
        return 0
    except PullError as exc:
        _say(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
