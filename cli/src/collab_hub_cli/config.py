"""Profiles: which hub a command talks to.

``config.toml`` in the configuration directory names one hub per profile and
which profile is the default::

    default_profile = "work"

    [profiles.work]
    hub = "https://hub.example.org"

A command's hub is ``--hub``, then ``COLLAB_HUB_URL``, then its profile's
``hub``; its profile is ``--profile``, then ``COLLAB_HUB_PROFILE``, then
``default_profile``, then ``default``. Typer reads the two environment
variables into the options, so this module sees only the resolved values.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PROFILE = "default"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class UsageError(Exception):
    """A command that cannot run as given: exit code 2."""


def config_dir() -> Path:
    """``COLLAB_HUB_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/collab-hub``, else ``~/.config/collab-hub``."""

    explicit = os.environ.get("COLLAB_HUB_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "collab-hub"


def check_profile_name(name: str) -> str:
    # The name becomes a file name under credentials/, so it may not reach outside it.
    if not _PROFILE_NAME.match(name):
        raise UsageError(f"invalid profile name {name!r}: use letters, digits, '.', '_' and '-'")
    return name


def is_loopback(host: str | None) -> bool:
    """Whether a host name stays on this machine: ``localhost``, ``*.localhost``, 127.0.0.0/8 or ``::1``."""

    if not host:
        return False
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def normalize_hub(url: str, insecure: bool = False) -> str:
    """The hub URL without a trailing slash; plain ``http`` only on this machine, unless ``insecure``.

    Over plain ``http`` elsewhere, anyone on the path could swap the issuer
    ``GET /v1/auth/cli`` names, and every bearer token would cross in clear.
    """

    url = url.strip().rstrip("/")
    if not re.match(r"^https?://[^/]", url):
        raise UsageError(f"the hub URL must start with http:// or https://, got {url!r}")
    if url.startswith("http://") and not insecure and not is_loopback(urlparse(url).hostname):
        raise UsageError(f"refusing {url}: plain http is only for a hub on this machine; use https, "
                         "or pass --insecure if you accept tokens crossing the network in clear")
    return url


def load(directory: Path) -> dict:
    path = directory / "config.toml"
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path} is not valid TOML: {exc}") from exc


def _quote(value: str) -> str:
    # A TOML basic string: JSON's escapes are a subset TOML accepts.
    return json.dumps(value, ensure_ascii=False)


def save(directory: Path, config: dict) -> None:
    lines = []
    if config.get("default_profile"):
        lines.append(f"default_profile = {_quote(config['default_profile'])}")
    for name, profile in sorted(config.get("profiles", {}).items()):
        lines += ["", f"[profiles.{_quote(name)}]"]
        lines += [f"{key} = {_quote(str(value))}" for key, value in sorted(profile.items())]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text("\n".join(lines).lstrip("\n") + "\n")


@dataclass(frozen=True)
class Target:
    """The profile and hub one command runs against."""

    directory: Path
    profile: str
    hub: str | None
    insecure: bool = False
    """``--insecure``: plain ``http`` accepted for a hub and a realm off this machine."""

    def require_hub(self) -> str:
        if not self.hub:
            raise UsageError(
                f"no hub for profile {self.profile!r}: pass --hub URL, set COLLAB_HUB_URL, "
                "or sign in once with `collab-hub --hub URL login`"
            )
        return self.hub


def resolve(hub: str | None, profile: str | None, directory: Path | None = None, insecure: bool = False) -> Target:
    directory = directory or config_dir()
    config = load(directory)
    name = check_profile_name(profile or config.get("default_profile") or DEFAULT_PROFILE)
    chosen = hub or config.get("profiles", {}).get(name, {}).get("hub")
    return Target(directory=directory, profile=name, hub=normalize_hub(chosen, insecure) if chosen else None,
                  insecure=insecure)


def remember(target: Target) -> None:
    """Record the target's hub under its profile, and make it the default when there is none."""

    config = load(target.directory)
    config.setdefault("profiles", {}).setdefault(target.profile, {})["hub"] = target.require_hub()
    config.setdefault("default_profile", target.profile)
    save(target.directory, config)
