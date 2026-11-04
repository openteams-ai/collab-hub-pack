#!/usr/bin/env python3
"""Fail if this Python's trust stores still hold a root from the eMudhra compromise.

    python scripts/check_emudhra_trust.py [--require-hardened-python]

Run inside an image (build-images.yaml does, before pushing the hub image) to check what its TLS
clients actually trust:

- the cafile and every file in the capath that ``ssl.get_default_verify_paths()`` reports, which is
  what ``urllib`` and a default ``SSLContext`` load (and where ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``
  point, when set);
- certifi's bundle, which httpx and requests use, when certifi is importable.

It fails if any certificate there matches a fingerprint in testdata/emudhra-incident-roots.txt (found
next to this script, so mounting the scripts directory is enough), or if it finds no certificates at
all. With ``--require-hardened-python``, it also fails unless this interpreter is at least the first
release of its minor version whose ``ssl`` module refuses the compromised intermediates whatever the
store holds (HARDENED_PYTHON, #285); a minor version not listed there fails too, until it is added.
Standard library only, so it runs in any image with a Python.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import ssl
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "testdata" / "emudhra-incident-roots.txt"
SHA256_HEX = re.compile(r"[0-9A-F]{64}")
PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----\s.+?\s-----END CERTIFICATE-----", re.DOTALL)
# The first CPython release of each minor version carrying gh-160040's ssl denylist for the
# compromised eMudhra intermediates. 3.10 and older got no backport.
HARDENED_PYTHON = {
    (3, 11): (3, 11, 18),
    (3, 12): (3, 12, 16),
    (3, 13): (3, 13, 18),
    (3, 14): (3, 14, 10),
    (3, 15): (3, 15, 2),
}


def load_fingerprints(path: Path) -> dict[str, str]:
    fingerprints = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, label = line.partition(" ")
        digest = digest.upper()
        # A malformed entry (a typo, or openssl's colon-separated form) would never match, so the
        # check would pass with that root still trusted; refuse it instead.
        if not SHA256_HEX.fullmatch(digest):
            raise SystemExit(f"error: {path}: not a 64-hex-digit SHA-256: {line!r}")
        fingerprints[digest] = label.strip() or digest
    if not fingerprints:
        raise SystemExit(f"error: no fingerprints in {path}")
    return fingerprints


def store_files() -> list[str]:
    paths = ssl.get_default_verify_paths()
    files = []
    for cafile in (paths.cafile, paths.openssl_cafile):
        if cafile and os.path.isfile(cafile):
            files.append(cafile)
    for capath in (paths.capath, paths.openssl_capath):
        if capath and os.path.isdir(capath):
            for name in sorted(os.listdir(capath)):
                full = os.path.join(capath, name)
                if os.path.isfile(full):
                    files.append(full)
    try:
        import certifi
    except ImportError:
        pass
    else:
        files.append(certifi.where())
    # A capath often links its hashed names to the same files; read each once.
    return list(dict.fromkeys(os.path.realpath(f) for f in files))


def python_failure(version: tuple[int, int, int]) -> str | None:
    hardened = HARDENED_PYTHON.get(version[:2])
    if hardened is None:
        return f"Python {version[0]}.{version[1]} has no hardened release listed in HARDENED_PYTHON"
    if version < hardened:
        return f"Python is older than {'.'.join(map(str, hardened))}, the first hardened {version[0]}.{version[1]}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require-hardened-python",
        action="store_true",
        help="also fail unless this interpreter carries CPython's eMudhra denylist",
    )
    args = parser.parse_args()

    fingerprints = load_fingerprints(FIXTURE)
    failures = []
    version = tuple(sys.version_info[:3])
    print(f"Python {'.'.join(map(str, version))}, {ssl.OPENSSL_VERSION}")
    if args.require_hardened_python:
        failure = python_failure(version)
        if failure:
            failures.append(failure)

    files = store_files()
    certificates = 0
    for path in files:
        text = Path(path).read_text(encoding="ascii", errors="ignore")
        for pem in PEM_CERT.findall(text):
            certificates += 1
            der = ssl.PEM_cert_to_DER_cert(pem)
            label = fingerprints.get(hashlib.sha256(der).hexdigest().upper())
            if label:
                failures.append(f"{path} trusts {label}")
    print(f"{certificates} certificates checked in {len(files)} files")
    if not certificates:
        failures.append("no certificates found in any trust store")

    sys.stdout.flush()
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
