"""File-type signatures and the logic each one uses to find where a carved file ends.

Carving works purely on bytes: a header marks where a file might start, and an
extent function tries to compute exactly how long it is, either by parsing the
format's own internal structure (exact) or by searching for a footer / capping
at a maximum size (best-effort). This is how classic carving tools like
PhotoRec and Scalpel work, and it's what lets this tool find files with no
filesystem metadata at all.
"""

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .source import Source

MB = 1024 * 1024
GB = 1024 * MB

# (length, truncated) -- truncated=True means we hit max_size or EOF without
# confirming a real end-of-file marker, so the recovered bytes may be incomplete.
ExtentResult = Tuple[int, bool]


@dataclass(frozen=True)
class Signature:
    name: str
    extension: str
    headers: Tuple[bytes, ...]
    max_size: int
    extent_fn: Callable[[Source, int, "Signature"], ExtentResult]
    min_size: int = 16  # discard matches that can't possibly be a real file this short
    header_back_offset: int = 0  # true file start is this many bytes before the header match


def _footer_search(source: Source, offset: int, sig: Signature, footer: bytes, use_last: bool = False) -> ExtentResult:
    window = source.read_at(offset, sig.max_size)
    idx = window.rfind(footer) if use_last else window.find(footer)
    if idx == -1:
        return len(window), True
    return idx + len(footer), False


def _jpeg_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    return _footer_search(source, offset, sig, b"\xff\xd9")


def _pdf_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    # Incrementally-updated PDFs can contain several "%%EOF" markers; the last
    # one in the window is the true end of the file.
    return _footer_search(source, offset, sig, b"%%EOF", use_last=True)


def _png_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    # PNG is a sequence of chunks: 4-byte length, 4-byte type, data, 4-byte CRC.
    # Walk them until IEND, so we don't stop early on a coincidental byte match.
    pos = 8  # past the 8-byte PNG signature
    while pos + 8 <= sig.max_size:
        header = source.read_at(offset + pos, 8)
        if len(header) < 8:
            return pos, True
        length = int.from_bytes(header[0:4], "big")
        chunk_type = header[4:8]
        pos += 8 + length + 4  # data + CRC
        if chunk_type == b"IEND":
            return pos, False
        if length < 0 or length > sig.max_size:
            return pos, True
    return sig.max_size, True


def _gif_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    # Walk GIF blocks (extensions and image data) rather than scanning for a
    # bare 0x3B trailer byte, which occurs constantly inside LZW image data.
    pos = 13  # 6-byte header + 7-byte logical screen descriptor
    screen = source.read_at(offset, 13)
    if len(screen) < 13:
        return len(screen), True
    if screen[10] & 0x80:  # global color table present
        pos += 3 * (2 << (screen[10] & 0x07))

    def skip_subblocks(p: int) -> Optional[int]:
        while True:
            b = source.read_at(offset + p, 1)
            if not b:
                return None
            n = b[0]
            p += 1
            if n == 0:
                return p
            p += n

    while pos < sig.max_size:
        b = source.read_at(offset + pos, 1)
        if not b:
            return pos, True
        marker = b[0]
        pos += 1
        if marker == 0x3B:  # trailer
            return pos, False
        elif marker == 0x21:  # extension introducer
            pos += 1  # label byte
            nxt = skip_subblocks(pos)
            if nxt is None:
                return pos, True
            pos = nxt
        elif marker == 0x2C:  # image descriptor
            desc = source.read_at(offset + pos, 9)
            if len(desc) < 9:
                return pos, True
            pos += 9
            if desc[8] & 0x80:  # local color table
                pos += 3 * (2 << (desc[8] & 0x07))
            pos += 1  # LZW minimum code size byte
            nxt = skip_subblocks(pos)
            if nxt is None:
                return pos, True
            pos = nxt
        else:
            return pos - 1, True
    return sig.max_size, True


def _riff_extent(source: Source, offset: int, sig: Signature, form_type: bytes) -> ExtentResult:
    # WAV/AVI: "RIFF" + 4-byte little-endian size (of everything after this
    # field) + 4-byte form type. Total length is exact if the field is sane.
    head = source.read_at(offset, 12)
    if len(head) < 12:
        return 0, True
    if head[8:12] != form_type:
        return 0, True  # same "RIFF" header, different container format
    chunk_size = int.from_bytes(head[4:8], "little")
    length = 8 + chunk_size
    if length < sig.min_size or length > sig.max_size:
        return sig.max_size, True
    return length, False


def _wav_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    return _riff_extent(source, offset, sig, b"WAVE")


def _avi_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    return _riff_extent(source, offset, sig, b"AVI ")


_BMP_DIB_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}


def _bmp_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    # "BM" is only 2 bytes, so it turns up constantly in random bytes. Reject
    # matches unless the rest of the header is internally consistent: a
    # plausible pixel-data offset, and a DIB header size that's one of the
    # handful of real values -- vanishingly unlikely to pass by chance.
    head = source.read_at(offset, 18)
    if len(head) < 18:
        return 0, True
    length = int.from_bytes(head[2:6], "little")
    data_offset = int.from_bytes(head[10:14], "little")
    dib_size = int.from_bytes(head[14:18], "little")
    if dib_size not in _BMP_DIB_HEADER_SIZES:
        return 0, True
    if data_offset < 14 + dib_size or data_offset > length:
        return 0, True
    if length < sig.min_size or length > sig.max_size:
        return sig.max_size, True
    return length, False


def _sqlite_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    head = source.read_at(offset, 32)
    if len(head) < 32:
        return len(head), True
    page_size = int.from_bytes(head[16:18], "big")
    if page_size == 1:
        page_size = 65536
    page_count = int.from_bytes(head[28:32], "big")
    length = page_size * page_count
    if page_size < 512 or length < sig.min_size or length > sig.max_size:
        return sig.max_size, True
    return length, False


def _zip_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    # ZIP (and docx/xlsx/pptx/jar/apk, which are ZIP containers): find the
    # End Of Central Directory record and use its comment-length field.
    window = source.read_at(offset, sig.max_size)
    idx = window.rfind(b"PK\x05\x06")
    if idx == -1:
        return len(window), True
    if idx + 22 > len(window):
        return len(window), True
    comment_len = int.from_bytes(window[idx + 20 : idx + 22], "little")
    length = idx + 22 + comment_len
    return min(length, len(window)), length > len(window)


def _mp4_extent(source: Source, offset: int, sig: Signature) -> ExtentResult:
    # MP4/MOV is a sequence of boxes: 4-byte size, 4-byte type, payload (and a
    # 64-bit size if the 32-bit one is 1). Walk top-level boxes to add them
    # up. Running into something that isn't a plausible box header is the
    # normal way this ends -- it means we've walked off the end of the real
    # file into whatever bytes follow it on disk, so `pos` so far is the
    # answer, not a truncation.
    pos = 0
    while pos + 8 <= sig.max_size:
        head = source.read_at(offset + pos, 8)
        if len(head) < 8:
            return pos, True  # ran into physical end of source: genuinely unknown
        size = int.from_bytes(head[0:4], "big")
        box_type = head[4:8]
        if not (box_type.isalnum() or box_type in (b"free", b"skip", b"wide")):
            return pos, False
        if size == 0:  # box extends to EOF of the original file -- can't know where that was
            return pos, True
        if size == 1:  # 64-bit extended size follows
            ext = source.read_at(offset + pos + 8, 8)
            if len(ext) < 8:
                return pos, True
            size = int.from_bytes(ext, "big")
        if size < 8:
            return pos, False
        if size > sig.max_size:
            return pos, True
        pos += size
    return pos, True


SIGNATURES: List[Signature] = [
    Signature("JPEG", "jpg", (b"\xff\xd8\xff",), 100 * MB, _jpeg_extent),
    Signature("PNG", "png", (b"\x89PNG\r\n\x1a\n",), 100 * MB, _png_extent),
    Signature("GIF", "gif", (b"GIF87a", b"GIF89a"), 50 * MB, _gif_extent, min_size=14),
    Signature("PDF", "pdf", (b"%PDF-",), 500 * MB, _pdf_extent),
    Signature("ZIP", "zip", (b"PK\x03\x04",), 2 * GB, _zip_extent, min_size=22),
    Signature("WAV", "wav", (b"RIFF",), 2 * GB, _wav_extent, min_size=44),
    Signature("AVI", "avi", (b"RIFF",), 4 * GB, _avi_extent, min_size=44),
    Signature("BMP", "bmp", (b"BM",), 100 * MB, _bmp_extent, min_size=54),
    Signature("SQLITE", "sqlite", (b"SQLite format 3\x00",), 20 * GB, _sqlite_extent),
    Signature("MP4", "mp4", (b"ftyp",), 10 * GB, _mp4_extent, min_size=8, header_back_offset=4),
]
