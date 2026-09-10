"""One-shot text-only PDF worker; invoked directly with isolated Python, never imported by the app."""

import ctypes
import io
import json
import math
import os
import sys

_JOB = None


def _windows_limits(memory_bytes: int, seconds: float) -> None:
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IOCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    global _JOB
    _JOB = kernel.CreateJobObjectW(None, None)
    if not _JOB:
        raise OSError("Could not establish a PDF job.")
    limits = ExtendedLimits()
    # Process memory, CPU time, no descendants, and kill-on-job-close are kernel
    # limits, not cooperative checks inside pypdf.
    limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2 | 0x8 | 0x2000
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    limits.BasicLimitInformation.PerProcessUserTimeLimit = math.ceil(seconds) * 10_000_000
    limits.ProcessMemoryLimit = memory_bytes
    if not kernel.SetInformationJobObject(_JOB, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise OSError("Could not limit the PDF job.")
    if not kernel.AssignProcessToJobObject(_JOB, kernel.GetCurrentProcess()):
        raise OSError("Could not isolate the PDF process.")
    # Keep the handle until process exit; closing this self-owned job kills it.


def _resource_limits(config: dict) -> None:
    if sys.platform == "win32":
        _windows_limits(config["memory_bytes"], config["timeout"])
    else:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (config["memory_bytes"], config["memory_bytes"]))
        seconds = max(1, math.ceil(config["timeout"]))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))


def _disable_external_actions() -> None:
    roots = tuple(os.path.normcase(os.path.realpath(root)) + os.sep for root in sys.path if root)

    def audit(event: str, args: tuple) -> None:
        if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event in {
            "os.system",
            "os.fork",
            "os.forkpty",
            "os.posix_spawn",
        }:
            raise PermissionError("External actions are disabled for PDF extraction.")
        if event == "open":
            filename, mode, flags = args
            if not isinstance(filename, (str, bytes, os.PathLike)):
                raise PermissionError("Document filesystem access is disabled.")
            path = os.path.normcase(os.path.realpath(filename))
            if isinstance(path, bytes):
                raise PermissionError("Document filesystem access is disabled.")
            if (
                (mode is not None and any(flag in mode for flag in "wax+"))
                or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                or not path.startswith(roots)
                or not path.endswith((".py", ".pyc", ".pyd", ".so", ".dll"))
            ):
                raise PermissionError("Document filesystem access is disabled.")

    sys.addaudithook(audit)


class _TextLimit(Exception):
    pass


def _extract(config: dict, body: bytes) -> dict:
    import pypdf
    import pypdf.filters

    # Recent pypdf versions expose these documented decompression safeguards.
    # The OS memory/CPU limits also cover older supported pypdf 6 releases.
    for name in (
        "ZLIB_MAX_OUTPUT_LENGTH",
        "LZW_MAX_OUTPUT_LENGTH",
        "RUN_LENGTH_MAX_OUTPUT_LENGTH",
        "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
        "MAX_DECLARED_STREAM_LENGTH",
        "JBIG2_MAX_OUTPUT_LENGTH",
        "FLATE_MAX_BUFFER_SIZE",
    ):
        if hasattr(pypdf.filters, name):
            setattr(pypdf.filters, name, min(getattr(pypdf.filters, name), 16_777_216))
    _disable_external_actions()
    reader = pypdf.PdfReader(io.BytesIO(body), strict=False)
    if reader.is_encrypted:
        return {"error": "pdf_encrypted"}
    page_count = len(reader.pages)
    blocks: list[dict] = []
    blank_pages: list[int] = []
    count, processed = 0, 0
    text_truncated = False
    for index in range(min(page_count, config["max_pages"])):
        remaining = config["max_chars"] - count
        if remaining <= 0:
            text_truncated = True
            break
        fragments: list[str] = []
        raw_count = 0

        def visitor(
            text: str, *_: object, budget: int = remaining, parts: list[str] = fragments
        ) -> None:
            nonlocal raw_count
            available = budget - raw_count
            if len(text) > available:
                parts.append(text[:available])
                raise _TextLimit
            parts.append(text)
            raw_count += len(text)

        try:
            reader.pages[index].extract_text(visitor_text=visitor, extraction_mode="plain")
        except _TextLimit:
            text_truncated = True
        text = " ".join("".join(fragments).replace("\x00", "").split())
        processed += 1
        if text:
            blocks.append({"text": text, "page": index + 1})
            count += len(text)
        else:
            blank_pages.append(index + 1)
        if text_truncated:
            break
    if not blocks:
        return {"error": "pdf_scanned"}
    title = reader.metadata.title if reader.metadata else ""
    if not isinstance(title, str):
        title = ""
    omitted_pages = max(0, page_count - processed)
    omissions = []
    if text_truncated:
        omissions.append("text_limit")
    if omitted_pages:
        omissions.append("unprocessed_pages")
    if blank_pages:
        omissions.append("pages_without_extractable_text_no_ocr")
    return {
        "title": title[:300],
        "blocks": blocks,
        "coverage": {
            "complete": not omissions,
            "truncated": text_truncated or bool(omitted_pages),
            "text_truncated": text_truncated,
            "total_pages": page_count,
            "pages_processed": processed,
            "pages_with_text": len(blocks),
            "pages_without_text": blank_pages,
            "omitted_pages": omitted_pages,
            "omissions": omissions,
            "extraction": "text_only_pdf",
            "ocr_performed": False,
        },
    }


def main() -> None:
    try:
        config = json.loads(sys.argv[1])
        _resource_limits(config)
    except Exception:
        sys.stdout.write('{"error":"pdf_isolation_unavailable"}')
        return
    sys.path.append(config["package_root"])
    try:
        body = sys.stdin.buffer.read(config["max_bytes"] + 1)
        if len(body) > config["max_bytes"]:
            result = {"error": "pdf_resource_limit"}
        else:
            result = _extract(config, body)
    except MemoryError:
        result = {"error": "pdf_resource_limit"}
    except Exception:
        # Parser diagnostics can contain document data and are not a public API.
        result = {"error": "pdf_parse_failed"}
    output = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
