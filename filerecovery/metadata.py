"""Extract file dates and generate thumbnails from raw carved bytes.

Both public functions are fully exception-safe: bad, truncated, or
corrupt bytes return None rather than raising, so callers never need
to wrap them in try/except.
"""

import io
import struct
from datetime import datetime
from typing import Optional

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

def extract_date(file_type: str, data: bytes) -> Optional[float]:
    """Return a Unix timestamp embedded in the file's metadata, or None.

    Falls back to None for any format that doesn't carry a creation date
    in its header (or when the header is absent/corrupt). The scanner
    uses the scan timestamp as a fallback when this returns None.
    """
    try:
        if file_type == "JPEG":
            return _jpeg_date(data)
        if file_type == "PNG":
            return _png_date(data)
        if file_type in ("MP4", "MOV"):
            return _mp4_date(data)
    except Exception:
        pass
    return None


def _parse_exif_datetime(s: str) -> Optional[float]:
    """Parse EXIF datetime string 'YYYY:MM:DD HH:MM:SS' → Unix timestamp."""
    s = s.rstrip("\x00").strip()
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _jpeg_date(data: bytes) -> Optional[float]:
    """Walk JPEG APP markers looking for APP1/EXIF; parse IFD for date tags."""
    # JPEG markers: FF XX [len_hi len_lo data...]
    # APP1 = FF E1, followed by "Exif\x00\x00" for EXIF data
    EXIF_TAGS = {0x9003, 0x0132}  # DateTimeOriginal, DateTime
    SEARCH_LIMIT = min(len(data), 128 * 1024)

    pos = 2  # skip SOI FF D8
    while pos + 4 <= SEARCH_LIMIT:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9):  # SOI / EOI
            pos += 2
            continue
        if pos + 4 > SEARCH_LIMIT:
            break
        seg_len = struct.unpack_from(">H", data, pos + 2)[0]
        seg_end = pos + 2 + seg_len
        if marker == 0xE1 and seg_len > 8:
            # Check for Exif header
            payload_start = pos + 4
            if data[payload_start:payload_start + 6] == b"Exif\x00\x00":
                result = _parse_exif_ifd(data, payload_start + 6, EXIF_TAGS)
                if result is not None:
                    return result
        pos = seg_end

    return None


def _parse_exif_ifd(data: bytes, tiff_start: int, target_tags: set) -> Optional[float]:
    """Parse a TIFF/EXIF IFD block and return the first matching date tag."""
    if tiff_start + 8 > len(data):
        return None

    byte_order_mark = data[tiff_start:tiff_start + 2]
    if byte_order_mark == b"II":
        bo = "<"
    elif byte_order_mark == b"MM":
        bo = ">"
    else:
        return None

    ifd_offset = struct.unpack_from(bo + "I", data, tiff_start + 4)[0]
    ifd_abs = tiff_start + ifd_offset
    if ifd_abs + 2 > len(data):
        return None

    entry_count = struct.unpack_from(bo + "H", data, ifd_abs)[0]
    entry_count = min(entry_count, 256)  # sanity cap

    for i in range(entry_count):
        entry_pos = ifd_abs + 2 + i * 12
        if entry_pos + 12 > len(data):
            break
        tag, typ, count = struct.unpack_from(bo + "HHI", data, entry_pos)
        if tag not in target_tags:
            continue
        if typ != 2:  # ASCII
            continue
        value_offset = struct.unpack_from(bo + "I", data, entry_pos + 8)[0]
        if count <= 4:
            # Value fits inline in the offset field
            raw = data[entry_pos + 8: entry_pos + 8 + count]
        else:
            abs_offset = tiff_start + value_offset
            raw = data[abs_offset: abs_offset + count]
        result = _parse_exif_datetime(raw.decode("ascii", errors="replace"))
        if result is not None:
            return result

    return None


def _png_date(data: bytes) -> Optional[float]:
    """Walk PNG chunks; extract creation date from tEXt 'Creation Time'."""
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    pos = 8
    while pos + 12 <= len(data):
        chunk_len = struct.unpack_from(">I", data, pos)[0]
        chunk_type = data[pos + 4: pos + 8]
        chunk_data = data[pos + 8: pos + 8 + chunk_len]
        pos += 12 + chunk_len

        if chunk_type == b"tEXt":
            sep = chunk_data.find(b"\x00")
            if sep == -1:
                continue
            key = chunk_data[:sep].decode("latin-1", errors="replace").lower()
            value = chunk_data[sep + 1:].decode("latin-1", errors="replace").strip()
            if key in ("creation time", "date:create", "date"):
                result = _parse_iso_date(value)
                if result is not None:
                    return result

        if chunk_type == b"IEND":
            break

    return None


def _parse_iso_date(s: str) -> Optional[float]:
    """Try common ISO 8601 date/datetime patterns."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).timestamp()
        except ValueError:
            continue
    return None


def _mp4_date(data: bytes) -> Optional[float]:
    """Read creation_time from MP4/MOV mvhd box (seconds since 1904-01-01)."""
    # Mac epoch is 1904-01-01; Unix epoch is 1970-01-01
    MAC_EPOCH_DELTA = 2082844800

    pos = 0
    while pos + 8 <= len(data):
        box_size = struct.unpack_from(">I", data, pos)[0]
        box_type = data[pos + 4: pos + 8]
        if box_size < 8:
            break
        if box_type == b"mvhd":
            # version byte at pos+8; if 0: 32-bit timestamps; if 1: 64-bit
            if pos + 9 > len(data):
                break
            version = data[pos + 8]
            if version == 0 and pos + 24 <= len(data):
                mac_ts = struct.unpack_from(">I", data, pos + 12)[0]
                return float(mac_ts - MAC_EPOCH_DELTA)
            elif version == 1 and pos + 32 <= len(data):
                mac_ts = struct.unpack_from(">Q", data, pos + 16)[0]
                return float(mac_ts - MAC_EPOCH_DELTA)
            break
        pos += box_size

    return None


# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

_IMAGE_TYPES = frozenset({"JPEG", "PNG", "GIF", "BMP"})


def make_thumbnail(file_type: str, data: bytes, size: int = 96) -> Optional[bytes]:
    """Return PNG-encoded thumbnail bytes, or None for non-image types / failures.

    Designed to be called from a background thread; never raises.
    """
    if not _PIL_AVAILABLE or file_type not in _IMAGE_TYPES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None
