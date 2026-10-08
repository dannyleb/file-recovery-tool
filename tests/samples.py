"""Builds small, byte-exact sample files of each supported type for tests."""

import io
import sqlite3
import struct
import wave
import zipfile
import zlib


def make_jpeg(payload_size: int = 200) -> bytes:
    body = bytes((i % 256) for i in range(payload_size))
    return b"\xff\xd8\xff\xe0" + body + b"\xff\xd9"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return length + chunk_type + data + crc


def make_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    idat = _png_chunk(b"IDAT", b"\x00" * 16)
    iend = _png_chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def make_gif() -> bytes:
    header = b"GIF89a"
    # width=1, height=1, packed=0x00 (no global color table), bg=0, aspect=0
    lsd = struct.pack("<HHBBB", 1, 1, 0x00, 0, 0)
    trailer = b"\x3b"
    return header + lsd + trailer


def make_pdf() -> bytes:
    body = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF"
    return body


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("hello.txt", "hello from the recovery tool test suite")
    return buf.getvalue()


def make_bmp(total_size: int = 100) -> bytes:
    assert total_size >= 54
    head = (
        b"BM"
        + struct.pack("<I", total_size)
        + b"\x00\x00\x00\x00"
        + struct.pack("<I", 54)   # pixel data offset
        + struct.pack("<I", 40)   # DIB header size (BITMAPINFOHEADER)
    )
    return head + bytes(total_size - len(head))


def make_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<10h", *range(10)))
    return buf.getvalue()


def make_sqlite() -> bytes:
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.remove(path)  # sqlite3.connect creates it fresh
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()
        conn.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


def make_mp4() -> bytes:
    ftyp_payload = b"isom\x00\x00\x02\x00isomiso2mp41"
    ftyp = struct.pack(">I", 8 + len(ftyp_payload)) + b"ftyp" + ftyp_payload
    mdat_payload = b"\x00" * 24
    mdat = struct.pack(">I", 8 + len(mdat_payload)) + b"mdat" + mdat_payload
    return ftyp + mdat
