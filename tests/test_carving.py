import os
import random
import tempfile

import pytest

from filerecovery.db import ScanDB
from filerecovery.recover import RecoveryError, recover_file
from filerecovery.scanner import scan
from filerecovery.source import Source

from . import samples


def _junk(n: int, rng: random.Random) -> bytes:
    return bytes(rng.randrange(256) for _ in range(n))


@pytest.fixture
def image(tmp_path):
    """A synthetic 'disk' with one sample of each type embedded in junk bytes,
    plus a deliberately placed JPEG header that straddles a chunk boundary."""
    rng = random.Random(1234)
    samples_by_name = {
        "JPEG": samples.make_jpeg(),
        "PNG": samples.make_png(),
        "GIF": samples.make_gif(),
        "PDF": samples.make_pdf(),
        "ZIP": samples.make_zip(),
        "BMP": samples.make_bmp(),
        "WAV": samples.make_wav(),
        "SQLITE": samples.make_sqlite(),
        "MP4": samples.make_mp4(),
    }

    chunks = []
    expected = []  # (type, offset, length)
    pos = 0

    def emit(data: bytes):
        nonlocal pos
        chunks.append(data)
        pos += len(data)

    emit(_junk(5000, rng))
    for name, blob in samples_by_name.items():
        offset = pos
        emit(blob)
        expected.append((name, offset, len(blob)))
        emit(_junk(rng.randrange(3000, 9000), rng))

    # Place one more JPEG so its header lands exactly across a chunk boundary
    # when scanned with a small chunk_size, to test the overlap logic.
    boundary_target = ((pos // 4096) + 1) * 4096 - 2  # header starts 2 bytes before a 4096 boundary
    pad = boundary_target - pos
    assert pad >= 0
    emit(_junk(pad, rng))
    straddling_jpeg = samples.make_jpeg(payload_size=50)
    straddle_offset = pos
    emit(straddling_jpeg)
    expected.append(("JPEG", straddle_offset, len(straddling_jpeg)))
    emit(_junk(4000, rng))

    path = tmp_path / "disk.img"
    with open(path, "wb") as f:
        for c in chunks:
            f.write(c)

    return str(path), expected, samples_by_name


def test_scan_finds_all_embedded_files(image):
    path, expected, samples_by_name = image
    db_path = os.path.join(os.path.dirname(path), "scan.db")

    with Source(path) as source, ScanDB(db_path) as db:
        scan_id = db.start_scan(path, source.size)
        # Small chunk_size forces several boundary crossings, including the
        # deliberately placed straddling JPEG header.
        found = scan(source, db, scan_id, chunk_size=4096)
        files = list(db.list_found_files(scan_id))

    assert found == len(expected)
    got = sorted((f.file_type, f.offset, f.length) for f in files)
    want = sorted(expected)
    assert got == want
    assert all(not f.truncated for f in files)


def test_recovered_bytes_match_originals_exactly(image):
    path, expected, samples_by_name = image
    db_path = os.path.join(os.path.dirname(path), "scan.db")
    dest = tempfile.mkdtemp(prefix="filerecovery-dest-")

    with Source(path) as source, ScanDB(db_path) as db:
        scan_id = db.start_scan(path, source.size)
        scan(source, db, scan_id, chunk_size=4096)
        files = list(db.list_found_files(scan_id))

        for f in files:
            dest_path = recover_file(source, db, f, dest)
            with open(dest_path, "rb") as rf:
                recovered_bytes = rf.read()
            if f.file_type == "JPEG":
                # two JPEGs exist; match by length against the known samples
                assert len(recovered_bytes) == f.length
                assert recovered_bytes[:4] == b"\xff\xd8\xff\xe0"
                assert recovered_bytes[-2:] == b"\xff\xd9"
            else:
                assert recovered_bytes == samples_by_name[f.file_type]


def test_recover_refuses_same_disk_destination(image):
    path, expected, samples_by_name = image
    db_path = os.path.join(os.path.dirname(path), "scan.db")
    source_dir = os.path.dirname(path)

    with Source(path) as source, ScanDB(db_path) as db:
        scan_id = db.start_scan(path, source.size)
        scan(source, db, scan_id, chunk_size=4096)
        f = next(iter(db.list_found_files(scan_id)))
        with pytest.raises(RecoveryError):
            recover_file(source, db, f, source_dir)


def test_resume_does_not_rescan_or_duplicate(image, tmp_path):
    path, expected, _ = image
    db_path = os.path.join(os.path.dirname(path), "scan.db")

    with Source(path) as source, ScanDB(db_path) as db:
        scan_id = db.start_scan(path, source.size)
        # Scan only the first half, then resume for the rest.
        halfway = source.size // 2
        scan(source, db, scan_id, chunk_size=4096, stop_check=_StopAfter(source, halfway))
        resume_from = db.resume_scan(scan_id)
        assert 0 < resume_from

        scan(source, db, scan_id, start_offset=resume_from, chunk_size=4096)
        files = list(db.list_found_files(scan_id))

    got = sorted((f.file_type, f.offset, f.length) for f in files)
    want = sorted(expected)
    assert got == want


class _StopAfter:
    def __init__(self, source, target):
        self.source = source
        self.target = target
        self.calls = 0

    def __call__(self):
        self.calls += 1
        # scan() checks stop_check() once per chunk read; stop once we've
        # done a couple of iterations past the target so some chunk started
        # beyond halfway, forcing a real resume rather than a no-op.
        return self.calls > (self.target // (4096)) + 2
