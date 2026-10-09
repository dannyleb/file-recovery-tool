"""Read-only access to a block device or disk image, with random access by offset."""

import fcntl
import os
import stat
import struct
import sys


def get_size(path: str) -> int:
    """Return the size in bytes of a file, disk image, or block/character device."""
    st = os.stat(path)
    if stat.S_ISREG(st.st_mode):
        return st.st_size

    # Block/character special device (e.g. /dev/rdisk2 on macOS, /dev/sdb on Linux):
    # st_size is usually 0, so we need platform-specific methods.
    fd = os.open(path, os.O_RDONLY)
    try:
        if sys.platform == "darwin":
            # macOS: lseek(SEEK_END) returns 0 on raw devices; use ioctl instead.
            DKIOCGETBLOCKCOUNT = 0x40086419
            DKIOCGETBLOCKSIZE  = 0x40046418
            block_count = struct.unpack("Q", fcntl.ioctl(fd, DKIOCGETBLOCKCOUNT, b"\x00" * 8))[0]
            block_size  = struct.unpack("I", fcntl.ioctl(fd, DKIOCGETBLOCKSIZE,  b"\x00" * 4))[0]
            return block_count * block_size
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


class Source:
    """A read-only, seekable handle on a disk/device/image, opened once and reused.

    Every read goes through read_at(offset, length), so callers never need to
    track a cursor themselves and random-access reads (for footer/box lookups)
    are safe to interleave with sequential scanning.
    """

    def __init__(self, path: str):
        self.path = path
        self.size = get_size(path)
        self._fh = open(path, "rb", buffering=0)

    # macOS raw devices reject single read() calls larger than ~8 MB (EINVAL).
    _MAX_READ = 8 * 1024 * 1024

    def read_at(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        # Raw devices on macOS require sector-aligned reads.
        sector = 512
        aligned_offset = (offset // sector) * sector
        pad = offset - aligned_offset

        # Read in _MAX_READ-aligned chunks to avoid EINVAL on large requests.
        chunks = []
        remaining = ((pad + length + sector - 1) // sector) * sector
        remaining = min(remaining, self.size - aligned_offset)
        pos = aligned_offset
        while remaining > 0:
            chunk_len = min(remaining, self._MAX_READ)
            # Keep chunk_len sector-aligned.
            chunk_len = (chunk_len // sector) * sector or sector
            chunk_len = min(chunk_len, remaining)
            self._fh.seek(pos)
            data = self._fh.read(chunk_len)
            if not data:
                break
            chunks.append(data)
            pos += len(data)
            remaining -= len(data)

        raw = b"".join(chunks)
        return raw[pad: pad + length]

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
