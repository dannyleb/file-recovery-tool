"""Carving engine: sequentially scans a source for file-signature headers and
records each one found, along with its computed extent, in a ScanDB."""

import time
from typing import Callable, Optional

from .db import ScanDB
from .signatures import SIGNATURES
from .source import Source

try:
    from .metadata import extract_date as _extract_date
except ImportError:
    _extract_date = None  # Pillow not installed; dates won't be extracted

DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024

ProgressCallback = Callable[[int, int], None]  # (bytes_scanned, total_size)


def _longest_header() -> int:
    return max(len(h) for sig in SIGNATURES for h in sig.headers)


def scan(
    source: Source,
    db: ScanDB,
    scan_id: int,
    start_offset: int = 0,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress_cb: Optional[ProgressCallback] = None,
    progress_interval: float = 2.0,
    stop_check: Optional[Callable[[], bool]] = None,
) -> int:
    """Scan source[start_offset:] for known file signatures, recording each
    find in db. Returns the number of files found this run.

    The scan is resumable: pass start_offset=db.resume_scan(scan_id) to pick
    up where a previous run left off (found_files from that run stay valid).
    """
    overlap = _longest_header() - 1
    pos = start_offset          # next byte eligible to start a NEW match
    read_pos = start_offset
    prev_tail = b""
    found_count = 0
    last_progress = time.monotonic()

    while read_pos < source.size:
        if stop_check and stop_check():
            break

        chunk = source.read_at(read_pos, chunk_size)
        if not chunk:
            if read_pos == start_offset:
                # First read returned nothing — almost always macOS Full Disk
                # Access blocking the terminal app even under sudo.
                raise PermissionError(
                    f"Read returned 0 bytes from {source.path} at offset 0.\n\n"
                    "On macOS, grant Full Disk Access to your terminal app:\n"
                    "  System Settings → Privacy & Security → Full Disk Access\n"
                    "Then relaunch your terminal and run the scan again with sudo."
                )
            break
        buf = prev_tail + chunk
        buf_start = read_pos - len(prev_tail)
        read_pos += len(chunk)

        matches = []
        for sig in SIGNATURES:
            for header in sig.headers:
                search_from = 0
                while True:
                    idx = buf.find(header, search_from)
                    if idx == -1:
                        break
                    matches.append((buf_start + idx, sig))
                    search_from = idx + 1
        matches.sort(key=lambda m: m[0])

        for match_offset, sig in matches:
            true_offset = match_offset - sig.header_back_offset
            if true_offset < pos:
                continue  # already inside a file we just recorded (or before start_offset)

            try:
                length, truncated = sig.extent_fn(source, true_offset, sig)
            except Exception as e:
                db.log_error(scan_id, e, offset=true_offset)
                continue
            if length < sig.min_size:
                continue  # extent function rejected this as a false positive

            file_date = None
            if _extract_date is not None:
                raw = source.read_at(true_offset, min(length, 65536))
                try:
                    file_date = _extract_date(sig.name, raw)
                except Exception:
                    pass

            db.add_found_file(scan_id, sig.name, sig.extension, true_offset, length, truncated, file_date)
            found_count += 1
            pos = true_offset + length

        prev_tail = chunk[-overlap:] if overlap > 0 else b""

        now = time.monotonic()
        if now - last_progress >= progress_interval:
            db.update_progress(scan_id, read_pos)
            if progress_cb:
                progress_cb(read_pos, source.size)
            last_progress = now

    db.update_progress(scan_id, read_pos)
    if progress_cb:
        progress_cb(read_pos, source.size)
    db.finish_scan(scan_id)
    return found_count
