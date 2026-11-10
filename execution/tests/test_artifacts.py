"""Cog artifacts: pulled by their pinned reference, verified, cached by the digest of their lock."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fake_registry import FakeRegistry

from collab_hub_execution import artifacts
from collab_hub_execution.artifacts import (
    ArtifactReference,
    PullError,
    RegistryClient,
    evict,
    fetch,
    is_reference,
    locate,
    materialize,
    package_files,
    publish,
    serve_command,
)

MANIFEST = '[workspace]\nname = "echo"\n\n[tasks]\nserve = "python serve.py"\n'


@pytest.fixture
def registry():
    registry = FakeRegistry().start()
    yield registry
    registry.stop()


@pytest.fixture
def private_registry():
    registry = FakeRegistry(credential="hub-cred:s3cret").start()
    yield registry
    registry.stop()


def package(tmp_path, name="echo", lock="version: 6\n"):
    directory = tmp_path / name
    directory.mkdir(parents=True)
    (directory / "pixi.toml").write_text(MANIFEST.replace("echo", name))
    (directory / "pixi.lock").write_text(lock)
    (directory / "serve.py").write_text("print('hi')\n")
    (directory / "COG.md").write_text("---\nname: echo\n---\n# echo\n")
    (directory / ".pixi").mkdir()
    (directory / ".pixi" / "env").write_text("not published")
    return directory


def client(registry, **settings):
    return RegistryClient(plain_http={registry.host}, **settings)


def test_a_pinned_reference_is_parsed_and_anything_else_is_not():
    reference = ArtifactReference.parse("hub.example.com:8443/cogs/echo@sha256:" + "a" * 64)
    assert (reference.host, reference.repository, reference.digest[:10]) == ("hub.example.com:8443", "cogs/echo",
                                                                               "sha256:aaa")
    assert str(reference) == "hub.example.com:8443/cogs/echo@sha256:" + "a" * 64
    assert is_reference("127.0.0.1:5000/cogs/echo@sha256:" + "b" * 64)
    for bad in ("echo", "hub/cogs/echo:1.0", "hub/cogs/echo@sha256:short", "hub/Cogs/echo@sha256:" + "a" * 64, 7):
        assert not is_reference(bad)
        with pytest.raises(PullError, match="not a pinned artifact reference"):
            ArtifactReference.parse(bad)


def test_a_configmap_mount_publishes_its_files_and_none_of_its_links(tmp_path, registry):
    """A package mounted from a ConfigMap is symbolic links into ``..data``, itself a link to a ``..<timestamp>``
    directory: the files are published under their own names, and neither hidden directory is a layer."""
    mount = tmp_path / "cog"
    versioned = mount / "..2026_11_10_15_14_49.3388200455"
    versioned.mkdir(parents=True)
    for name, text in (("pixi.toml", MANIFEST), ("pixi.lock", "version: 6\n"), ("serve.py", "print('hi')\n"),
                       ("COG.md", "# echo\n")):
        (versioned / name).write_text(text)
        (mount / name).symlink_to(Path("..data") / name)
    (mount / "..data").symlink_to(versioned.name, target_is_directory=True)

    assert [p.name for p in package_files(mount)] == ["COG.md", "pixi.lock", "pixi.toml", "serve.py"]
    reference = publish(mount, f"{registry.host}/cogs/echo:cm", client(registry))
    pulled = materialize(reference, tmp_path / "cache", client(registry))
    pulled_files = sorted(p.name for p in pulled.directory.iterdir() if not p.name.startswith("."))
    assert pulled_files == ["COG.md", "pixi.lock", "pixi.toml", "serve.py"]  # beside the puller's own marker
    assert (pulled.directory / "pixi.toml").read_text() == MANIFEST


def test_a_package_is_published_and_pulled_back_byte_for_byte_into_the_cache(tmp_path, registry):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:0.1.0", client(registry))
    assert reference.host == registry.host and reference.repository == "cogs/echo"
    manifest = json.loads(registry.manifests[("cogs/echo", reference.digest)])
    titles = {layer["annotations"]["org.opencontainers.image.title"]: layer["mediaType"]
              for layer in manifest["layers"]}
    assert titles == {"COG.md": artifacts.MEDIA_TYPE_NEBI_ASSET, "pixi.toml": artifacts.MEDIA_TYPE_PIXI_TOML,
                      "pixi.lock": artifacts.MEDIA_TYPE_PIXI_LOCK, "serve.py": artifacts.MEDIA_TYPE_NEBI_ASSET}
    assert manifest["config"]["mediaType"] == artifacts.MEDIA_TYPE_PIXI_CONFIG
    cache = tmp_path / "cache"
    said = []
    pulled = materialize(reference, cache, client(registry), log=said.append)
    assert not pulled.cached and pulled.serve == "python serve.py"
    assert pulled.directory == cache / "packages" / artifacts._hex(pulled.lock_digest)
    for name in ("pixi.toml", "pixi.lock", "serve.py", "COG.md"):
        assert (pulled.directory / name).read_bytes() == (tmp_path / "echo" / name).read_bytes()
    assert not (pulled.directory / ".pixi").exists()  # the environment is never published
    assert any(line.startswith("artifact pulling") for line in said)
    assert serve_command(pulled) == ["pixi", "run", "--locked", "--manifest-path", str(pulled.manifest), "serve"]
    # The lock's digest is the key: its layer's digest, which is the sha256 of the lock's bytes.
    import hashlib
    assert pulled.lock_digest == "sha256:" + hashlib.sha256(b"version: 6\n").hexdigest()


def test_a_second_materialization_of_the_same_lock_pulls_nothing_and_finds_the_package(tmp_path, registry):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:0.1.0", client(registry))
    cache = tmp_path / "cache"
    first = materialize(reference, cache, client(registry))
    (first.directory / ".pixi").mkdir()  # what pixi builds beside the package on its first run
    registry.blob_reads.clear()
    said = []
    again = materialize(reference, cache, client(registry), log=said.append)
    assert again.cached and again.directory == first.directory and registry.blob_reads == []
    assert any(line.startswith("artifact cached") for line in said)
    assert (again.directory / ".pixi").is_dir()  # the environment built on the first run is still there
    # Another artifact with the same lock (a new serve.py, say) shares the package's environment: same key.
    other = package(tmp_path / "v2", lock="version: 6\n")
    (other / "serve.py").write_text("print('changed')\n")
    second = publish(other, f"{registry.host}/cogs/echo:0.2.0", client(registry))
    assert second.digest != reference.digest
    shared = materialize(second, cache, client(registry))
    assert shared.cached and shared.directory == first.directory
    assert locate(second, cache).directory == first.directory and locate(reference, cache).cached


def test_a_changed_lock_is_another_package(tmp_path, registry):
    first = publish(package(tmp_path, "a"), f"{registry.host}/cogs/a:1", client(registry))
    second = publish(package(tmp_path, "b", lock="version: 7\n"), f"{registry.host}/cogs/b:1", client(registry))
    cache = tmp_path / "cache"
    one, two = materialize(first, cache, client(registry)), materialize(second, cache, client(registry))
    assert one.directory != two.directory and not two.cached


def test_a_blob_that_does_not_match_its_digest_is_refused_and_nothing_is_kept(tmp_path, registry):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:1", client(registry))
    manifest = json.loads(registry.manifests[("cogs/echo", reference.digest)])
    serve_digest = next(layer["digest"] for layer in manifest["layers"]
                        if layer["annotations"]["org.opencontainers.image.title"] == "serve.py")
    registry.corrupt(serve_digest)
    cache = tmp_path / "cache"
    with pytest.raises(PullError, match="does not match its digest"):
        materialize(reference, cache, client(registry))
    assert not (cache / "packages").exists() or not list((cache / "packages").iterdir())
    with pytest.raises(PullError, match="not been materialized"):
        locate(reference, cache)


def test_a_manifest_served_under_the_wrong_digest_is_refused(tmp_path, registry):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:1", client(registry))
    wrong = ArtifactReference(reference.host, reference.repository, "sha256:" + "0" * 64)
    registry.manifests[("cogs/echo", wrong.digest)] = registry.manifests[("cogs/echo", reference.digest)]
    with pytest.raises(PullError, match="does not match its digest"):
        materialize(wrong, tmp_path / "cache", client(registry))
    missing = ArtifactReference(reference.host, reference.repository, "sha256:" + "1" * 64)
    with pytest.raises(PullError, match="not in the registry"):
        materialize(missing, tmp_path / "cache", client(registry))


def test_a_package_without_a_lock_or_a_serve_task_is_not_runnable(tmp_path, registry):
    directory = package(tmp_path)
    (directory / "pixi.lock").unlink()
    with pytest.raises(PullError, match="needs pixi.toml and pixi.lock"):
        publish(directory, f"{registry.host}/cogs/echo:1", client(registry))
    directory = package(tmp_path / "no-serve")
    (directory / "pixi.toml").write_text('[workspace]\nname = "x"\n')
    reference = publish(directory, f"{registry.host}/cogs/x:1", client(registry))
    with pytest.raises(PullError, match="declares no `serve` task"):
        materialize(reference, tmp_path / "cache", client(registry))
    with pytest.raises(PullError, match="declares no `serve` task"):
        fetch(reference, client(registry))


def test_a_layer_titled_outside_the_package_is_refused(tmp_path, registry):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:1", client(registry))
    manifest = json.loads(registry.manifests[("cogs/echo", reference.digest)])
    manifest["layers"][0]["annotations"]["org.opencontainers.image.title"] = "../escape.py"
    body = json.dumps(manifest).encode()
    import hashlib
    evil = ArtifactReference(reference.host, reference.repository, "sha256:" + hashlib.sha256(body).hexdigest())
    registry.manifests[("cogs/echo", evil.digest)] = body
    with pytest.raises(PullError, match="outside the package"):
        materialize(evil, tmp_path / "cache", client(registry))


def test_fetch_reads_the_card_and_the_manifest_but_never_the_lock(tmp_path, registry):
    reference = publish(package(tmp_path, lock="version: 6\n" + "x" * 50000), f"{registry.host}/cogs/echo:1",
                        client(registry))
    registry.blob_reads.clear()
    fetched = fetch(str(reference), client(registry))
    assert set(fetched.files) == {"COG.md", "pixi.toml"} and fetched.serve == "python serve.py"
    assert fetched.titles == ("COG.md", "pixi.lock", "pixi.toml", "serve.py")
    assert fetched.lock_digest.startswith("sha256:") and fetched.lock_digest not in registry.blob_reads
    assert len(registry.blob_reads) == 2


def test_a_private_registry_is_pulled_from_with_the_credential_through_the_token_flow(tmp_path, private_registry):
    reference = publish(package(tmp_path), f"{private_registry.host}/cogs/echo:1",
                        client(private_registry, credential="hub-cred:s3cret"))
    pulled = materialize(reference, tmp_path / "cache", client(private_registry, credential="hub-cred:s3cret"))
    assert not pulled.cached and (pulled.directory / "serve.py").is_file()
    assert ("GET", "/token") in private_registry.requests
    with pytest.raises(PullError, match="refused the credential"):
        materialize(reference, tmp_path / "other", client(private_registry, credential="hub-cred:wrong"))
    with pytest.raises(PullError, match="offered no bearer challenge|refused the credential"):
        materialize(reference, tmp_path / "other", client(private_registry))


def test_evicting_a_reference_removes_its_package_unless_another_shares_the_lock(tmp_path, registry):
    first = publish(package(tmp_path, "a"), f"{registry.host}/cogs/a:1", client(registry))
    cache = tmp_path / "cache"
    pulled = materialize(first, cache, client(registry))
    assert evict(first, cache, keep_lock=True) is False and pulled.directory.is_dir()
    with pytest.raises(PullError):
        locate(first, cache)
    materialize(first, cache, client(registry))
    assert evict(first, cache) is True and not pulled.directory.exists()
    assert evict(first, cache) is False


def test_the_command_line_materializes_and_pulls(tmp_path, registry, monkeypatch, capsys):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:1", client(registry))
    monkeypatch.setenv(artifacts.PLAIN_HTTP_ENV, registry.host)
    cache = tmp_path / "cache"
    assert artifacts.main(["materialize", str(reference), "--cache", str(cache)]) == 0
    assert capsys.readouterr().out.strip() == str(locate(reference, cache).directory)
    assert artifacts.main(["pull", str(reference), "--into", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "serve.py").read_text() == "print('hi')\n"
    assert artifacts.main(["publish", str(tmp_path / "echo"), f"{registry.host}/cogs/echo:2"]) == 0
    assert capsys.readouterr().out.strip() == str(reference)  # the same files: the same digest
    assert artifacts.main(["materialize", "not-a-reference", "--cache", str(cache)]) == 1
    assert "not a pinned artifact reference" in capsys.readouterr().err


def test_serve_execs_pixi_on_the_materialized_package(tmp_path, registry, monkeypatch):
    reference = publish(package(tmp_path), f"{registry.host}/cogs/echo:1", client(registry))
    monkeypatch.setenv(artifacts.PLAIN_HTTP_ENV, registry.host)
    cache = tmp_path / "cache"
    materialize(reference, cache, client(registry))
    executed = []
    monkeypatch.setattr(artifacts.os, "execvp", lambda file, args: executed.append((file, list(args))))
    monkeypatch.setattr(artifacts.os, "chdir", lambda path: executed.append(("chdir", str(path))))
    artifacts.main(["serve", str(reference), "--cache", str(cache), "--pixi", sys.executable])
    package_dir = locate(reference, cache).directory
    assert executed == [("chdir", str(package_dir)), (sys.executable, [
        sys.executable, "run", "--locked", "--manifest-path", str(package_dir / "pixi.toml"), "serve"])]
