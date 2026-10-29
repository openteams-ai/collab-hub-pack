from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import sys
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# PDF text extraction for the SharePoint read route.
#
# Microsoft Graph converts Office documents (docx/pptx/xlsx/...) to PDF
# server-side; PDFs are downloaded as-is. Either way the Hub turns PDF bytes
# into bounded plain text here, so raw provider bytes never leave the Hub --
# the same rule Drive and Gmail follow. The engine is PDFium (via pypdfium2),
# the same library the Collab desktop app's OCR sidecar uses for attached PDFs.
#
# This parses untrusted input, and the bounds come in two layers. The byte
# size was capped before download, and the page count and character budget are
# capped in extract_pdf_text. But those bound what is downloaded and returned,
# not what the parse itself costs: a small, highly compressible PDF can make
# PDFium take hundreds of megabytes or tens of seconds. So the API process
# never parses a file itself. extract_pdf_text_async runs each parse in a
# separate worker process (sharepoint_pdf_worker) with a memory ceiling and a
# CPU ceiling, under a real deadline after which the worker is killed and
# reaped, and one parse at a time. An adverse file costs one dead worker and
# an "unsupported" answer; the Hub keeps serving. Scanned pages (images, no
# text layer) extract to nothing; OCR is deliberately out of scope for the Hub.

# PDFium is not thread-safe (pypdfium2's "Incompatibility with Threading"):
# two parses at once in one process can crash it, even on different documents.
# The worker is one process per parse, so this only matters for in-process
# callers of extract_pdf_text (the worker itself, and tests).
_PDFIUM_LOCK = threading.Lock()

# Two gates, one permit each, bound what document reads cost the API process:
#
# * "admission" is taken by the SharePoint client BEFORE a PDF or Office body
#   is downloaded or converted and held until the parse ends, so a read that
#   is waiting its turn holds no body. The pod's cost for document reads is
#   one body plus one worker at a time, however many reads arrive. The wait
#   is bounded (ExtractionBusy), and the permit is released on failure and
#   on cancellation.
# * "parse" is taken around the worker itself: belt and braces for a caller
#   that reaches extract_pdf_text_async without a permit.
#
# asyncio primitives bind to the first loop that waits on them, so the gates
# are kept per event loop (one loop per API process; one per test).
_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


def _gate(name: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gates = _GATES.get(loop)
    if gates is None:
        gates = _GATES[loop] = {}
    gate = gates.get(name)
    if gate is None:
        gate = gates[name] = asyncio.Semaphore(1)
    return gate


class ExtractionBusy(RuntimeError):
    """Another document read holds the admission permit and the bounded wait ran out."""


@asynccontextmanager
async def extraction_permit(wait_seconds: float) -> AsyncIterator[None]:
    """Admit one PDF/Office read at a time, from before its download to after its parse.

    Raises :class:`ExtractionBusy` when the permit is not free within
    ``wait_seconds``. The permit is released however the body ends, including
    cancellation: a client that gives up while waiting takes nothing with it.
    """
    gate = _gate("admission")
    if not gate.locked():
        await gate.acquire()  # free, and nobody queued ahead: taken without suspending
    elif wait_seconds <= 0:
        raise ExtractionBusy("another document is being read")
    else:
        try:
            await asyncio.wait_for(gate.acquire(), wait_seconds)
        except TimeoutError:
            raise ExtractionBusy("another document is being read") from None
    try:
        yield
    finally:
        gate.release()


_WORKER_MODULE = "collab_hub_api.connectors.sharepoint_pdf_worker"
# The whole environment a worker starts with. The API process holds every
# secret the deployment injects (database URLs, broker client secrets, cloud
# credentials); a process that parses untrusted bytes with a native library
# must not inherit any of it, so the worker gets this and nothing else.
# sys.executable is the venv interpreter and the package is installed into
# it, so the worker needs no PATH, PYTHONPATH or HOME to import and run.
_WORKER_ENVIRONMENT = {
    "PATH": os.defpath,
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}
# Largest reply the parent will read from a worker: max_chars of text plus the
# JSON around it. Anything bigger is a misbehaving worker, not a document, and
# is treated like a stall: killed, never buffered.
_MAX_WORKER_OUTPUT_BYTES = 8 * 1024 * 1024


class PdfExtractionError(RuntimeError):
    """The bytes could not be parsed as a PDF, or the parse could not be completed safely."""


def extract_pdf_text(data: bytes, *, max_chars: int, max_pages: int) -> tuple[str, bool]:
    """Plain text of a PDF, page by page, stopping at ``max_chars``/``max_pages``.

    Returns ``(text, truncated)``. ``truncated`` is True when pages were left
    unread or the assembled text exceeded ``max_chars`` and was cut. This runs
    PDFium in the calling process; the route uses :func:`extract_pdf_text_async`,
    which runs it in a bounded worker instead.
    """
    with _PDFIUM_LOCK:
        return _extract_pdf_text(data, max_chars=max_chars, max_pages=max_pages)


def _extract_pdf_text(data: bytes, *, max_chars: int, max_pages: int) -> tuple[str, bool]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - dependency is pinned in pyproject
        raise PdfExtractionError("PDF text extraction is not available on this hub") from exc

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise PdfExtractionError("not a readable PDF") from exc
    except Exception as exc:  # noqa: BLE001 - any parser failure on untrusted bytes is "unreadable"
        raise PdfExtractionError("not a readable PDF") from exc

    parts: list[str] = []
    truncated = False
    chars = 0
    try:
        page_count = len(document)
        if page_count > max_pages:
            truncated = True
        for index in range(min(page_count, max_pages)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    page_text = text_page.get_text_range()
                finally:
                    text_page.close()
            except pdfium.PdfiumError:
                page_text = ""
            finally:
                page.close()
            page_text = _normalize_text(page_text)
            if page_text:
                parts.append(page_text)
                chars += len(page_text) + 2
            if chars > max_chars:
                # Overshoot slightly; the slice below sets the final truncation.
                # Stopping here bounds parse work, not just the returned size.
                if index + 1 < page_count:
                    truncated = True
                break
    finally:
        document.close()

    assembled = "\n\n".join(parts).strip()
    if len(assembled) > max_chars:
        truncated = True
    return assembled[:max_chars], truncated


async def extract_pdf_text_async(
    data: bytes,
    *,
    max_chars: int,
    max_pages: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> tuple[str, bool]:
    """Parse in a bounded worker process, one at a time, under a real deadline.

    Raises ``TimeoutError`` when the worker overruns ``timeout_seconds``: it is
    killed and reaped first, so no parse outlives its deadline and the next
    read starts clean. Raises :class:`PdfExtractionError` when the worker could
    not read the file, hit its memory ceiling, or died.
    """
    async with _gate("parse"):
        return await _run_worker(
            data,
            max_chars=max_chars,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
            memory_limit_bytes=memory_limit_bytes,
        )


def worker_environment() -> dict[str, str]:
    """The complete environment of one parse worker: fixed, and free of the API's secrets.

    On Windows (development only) the interpreter also needs the Windows
    directory to start; nothing else from the parent is passed through.
    """
    env = dict(_WORKER_ENVIRONMENT)
    if sys.platform == "win32":  # pragma: no cover - exercised on developer laptops only
        for name in ("SYSTEMROOT", "SYSTEMDRIVE"):
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


# prctl(2) operations: PR_SET_DUMPABLE / PR_GET_DUMPABLE.
_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4
_inspection_denied = False


def deny_parent_inspection() -> bool:
    """Make this process non-dumpable (Linux), so the worker cannot inspect it.

    A fixed ``env=`` stops the worker from inheriting the API's secrets, but
    the worker runs as the same Linux user, and Linux lets a user read its
    own processes' initial environment, memory and open files through
    ``/proc/<pid>/{environ,mem,maps,fd}`` and ptrace. A non-dumpable process
    is exempt: the kernel refuses those to everything but root, while the
    process itself keeps full access to its own ``/proc/self``. The cost is
    that the API process writes no core dumps and cannot be attached to by a
    same-user debugger, both right for a process holding deployment secrets.

    Idempotent. Returns True once the property holds; False where it cannot
    be set (not Linux, or prctl refused), which callers treat as fail-closed.
    """
    global _inspection_denied
    if sys.platform != "linux":
        return False
    if _inspection_denied:
        return True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return False
        _inspection_denied = libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):  # pragma: no cover - no usable libc
        return False
    return _inspection_denied


def worker_command(*, max_chars: int, max_pages: int, memory_limit_bytes: int, cpu_seconds: int) -> list[str]:
    """The argv of one parse worker. Separate so tests can substitute a stand-in."""
    return [
        sys.executable,
        "-m",
        _WORKER_MODULE,
        str(max_chars),
        str(max_pages),
        str(memory_limit_bytes),
        str(cpu_seconds),
    ]


async def _run_worker(
    data: bytes,
    *,
    max_chars: int,
    max_pages: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> tuple[str, bool]:
    # Before the first worker ever exists, this process stops being readable
    # by its own user: a worker that is taken over by a file must find neither
    # secrets in its environment (env= below) nor in ours (non-dumpable).
    if sys.platform == "linux" and not deny_parent_inspection():
        raise PdfExtractionError("the API process could not be shielded from the parse worker")
    # The CPU ceiling is a second line behind the wall-clock deadline, for a
    # worker the parent could not kill; a little over the deadline so a normal
    # parse never trips it first.
    cpu_seconds = max(1, math.ceil(timeout_seconds) + 1)
    process = await asyncio.create_subprocess_exec(
        *worker_command(
            max_chars=max_chars,
            max_pages=max_pages,
            memory_limit_bytes=memory_limit_bytes,
            cpu_seconds=cpu_seconds,
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=worker_environment(),
    )
    try:
        stdout = await asyncio.wait_for(_exchange(process, data), timeout_seconds)
    except (TimeoutError, _ReplyTooLarge):
        # Overran, or wrote more than a document can produce. Kill and reap,
        # so the deadline is the end of the work, not just of the wait.
        await _kill(process)
        raise TimeoutError("PDF extraction exceeded its time budget") from None
    except BaseException:
        await _kill(process)
        raise
    if process.returncode != 0:
        # Killed by its own memory or CPU ceiling, or crashed inside PDFium.
        raise PdfExtractionError("the file could not be read within the extraction limits")
    try:
        reply = json.loads(stdout.decode("utf-8"))
    except ValueError as exc:
        raise PdfExtractionError("the extraction worker returned no result") from exc
    if not isinstance(reply, dict):
        raise PdfExtractionError("the extraction worker returned no result")
    if "error" in reply:
        raise PdfExtractionError(str(reply["error"]))
    text = reply.get("text")
    if not isinstance(text, str):
        raise PdfExtractionError("the extraction worker returned no result")
    return text[:max_chars], bool(reply.get("truncated"))


class _ReplyTooLarge(Exception):
    pass


async def _exchange(process: asyncio.subprocess.Process, data: bytes) -> bytes:
    # Like Process.communicate, but the reply is read up to a ceiling instead
    # of whole: a worker that floods stdout is cut off and killed by the caller
    # rather than buffered in the API process.
    async def feed() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the worker ended before reading everything; its exit code says why
        process.stdin.close()

    async def collect() -> bytes:
        # StreamReader.read(n) returns whatever is buffered, not n bytes, so
        # a reply is assembled chunk by chunk until EOF or the ceiling.
        assert process.stdout is not None
        chunks: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(64 * 1024):
            size += len(chunk)
            if size > _MAX_WORKER_OUTPUT_BYTES:
                raise _ReplyTooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    _, reply = await asyncio.gather(feed(), collect())
    await process.wait()
    return reply


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    # Reap it: a killed worker that is never waited on stays a zombie in the
    # pod until the API process exits. Process.wait() also waits for the
    # pipes to close, so close our end of stdin and read stdout to its end:
    # a reader paused by flow control under a flood would never see EOF.
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(asyncio.gather(_discard(process.stdout), process.wait()), 10)
    except TimeoutError:  # pragma: no cover - the kernel does not refuse SIGKILL
        pass


async def _discard(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    try:
        while await stream.read(64 * 1024):
            pass
    except (OSError, ValueError):  # pragma: no cover - the pipe is already gone
        pass


def _normalize_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line and compact and compact[-1] == "":
            continue
        compact.append(line)
    return "\n".join(compact).strip()
