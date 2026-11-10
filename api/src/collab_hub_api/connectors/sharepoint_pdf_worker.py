"""Isolated PDF text extraction for the SharePoint read route.

Run as ``python -m collab_hub_api.connectors.sharepoint_pdf_worker MAX_CHARS
MAX_PAGES MEMORY_LIMIT_BYTES CPU_SECONDS`` with the PDF bytes on stdin. Writes
one JSON object to stdout: ``{"text": ..., "truncated": ...}`` or
``{"error": ...}``.

The parent (``sharepoint_text.extract_pdf_text_async``) owns the deadline and
kills this process when it overruns. What this process adds is the ceiling the
API process itself cannot impose on a parse: on POSIX the address-space and CPU
limits are set here, before PDFium loads, so an adverse file ends with this
worker and never with the Hub. Only this module and pypdfium2 are imported, so
a worker costs an interpreter start, not an application start.
"""

from __future__ import annotations

import json
import sys


def _apply_limits(memory_limit_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no rlimits; the parent's kill still applies
        return
    for name, limit in (("RLIMIT_AS", memory_limit_bytes), ("RLIMIT_CPU", cpu_seconds)):
        kind = getattr(resource, name, None)
        if kind is None or limit <= 0:
            continue
        soft, hard = resource.getrlimit(kind)
        wanted = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        try:
            resource.setrlimit(kind, (wanted, hard))
        except (ValueError, OSError):  # pragma: no cover - keep the parent's kill as the backstop
            pass


def main(argv: list[str]) -> int:
    max_chars, max_pages, memory_limit_bytes, cpu_seconds = (int(value) for value in argv[1:5])
    _apply_limits(memory_limit_bytes, cpu_seconds)
    data = sys.stdin.buffer.read()
    try:
        from collab_hub_api.connectors.sharepoint_text import PdfExtractionError, extract_pdf_text

        text, truncated = extract_pdf_text(data, max_chars=max_chars, max_pages=max_pages)
    except MemoryError:
        sys.stdout.write(json.dumps({"error": "the file needs more memory than a read is allowed"}))
        return 0
    except PdfExtractionError as exc:
        sys.stdout.write(json.dumps({"error": str(exc)}))
        return 0
    sys.stdout.write(json.dumps({"text": text, "truncated": truncated}))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main(sys.argv))
