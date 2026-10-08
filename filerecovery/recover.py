"""Copy carved-file byte ranges out of the source and onto a destination disk."""

import os
import stat

from .db import FoundFile, ScanDB
from .source import Source

COPY_CHUNK = 4 * 1024 * 1024


class RecoveryError(Exception):
    pass


def _same_disk(source_path: str, dest_dir: str) -> bool:
    src_stat = os.stat(source_path)
    dest_stat = os.stat(dest_dir)

    if stat.S_ISREG(src_stat.st_mode):
        # Source is a plain disk-image file: treat its containing directory
        # as "the disk" for safety purposes.
        src_dir = os.path.realpath(os.path.dirname(source_path) or ".")
        dest_abs = os.path.realpath(dest_dir)
        return dest_abs == src_dir or dest_abs.startswith(src_dir + os.sep)

    # Source is a raw block/character device. This is a best-effort check --
    # it compares device numbers, which lines up for simple unpartitioned
    # disks but can't see through every volume-manager/container setup.
    # Recovering to a *different physical drive* is the only real guarantee.
    return getattr(src_stat, "st_rdev", None) == dest_stat.st_dev


def recover_file(source: Source, db: ScanDB, found: FoundFile, dest_dir: str) -> str:
    """Write found's byte range to dest_dir and mark it recovered. Returns the
    path written. Refuses to write back onto the source path/device."""
    os.makedirs(dest_dir, exist_ok=True)
    if _same_disk(source.path, dest_dir):
        raise RecoveryError(
            "destination is on the source disk -- recovering onto the same disk "
            "risks overwriting other deleted data you haven't recovered yet"
        )

    name = f"{found.file_type.lower()}_{found.offset:012d}.{found.extension}"
    dest_path = os.path.join(dest_dir, name)

    remaining = found.length
    offset = found.offset
    with open(dest_path, "wb") as out:
        while remaining > 0:
            chunk = source.read_at(offset, min(COPY_CHUNK, remaining))
            if not chunk:
                break
            out.write(chunk)
            offset += len(chunk)
            remaining -= len(chunk)

    db.mark_recovered(found.id, dest_path)
    return dest_path
