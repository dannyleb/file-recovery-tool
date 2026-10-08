"""Read-only access to a block device or disk image, with random access by offset."""

import os
import stat


def get_size(path: str) -> int:
    """Return the size in bytes of a file, disk image, or block/character device."""
    st = os.stat(path)
    if stat.S_ISREG(st.st_mode):
        return st.st_size

    # Block/character special device (e.g. /dev/rdisk2 on macOS, /dev/sdb on Linux):
    # st_size is usually 0, so seek to the end to find the real size.
    fd = os.open(path, os.O_RDONLY)
    try:
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

    def read_at(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        self._fh.seek(offset)
        data = self._fh.read(length)
        return data

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
