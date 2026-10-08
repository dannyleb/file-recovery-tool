"""SQLite storage for scan progress and found-file records.

Scans run against multi-terabyte drives and can take hours, so progress and
results are persisted incrementally -- a scan can be interrupted and resumed,
and results can be browsed/recovered long after the scan finished.
"""

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    bytes_scanned INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS found_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    file_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL,
    truncated INTEGER NOT NULL,
    recovered_path TEXT,
    recovered_at REAL
);

CREATE INDEX IF NOT EXISTS idx_found_files_scan ON found_files(scan_id);
"""


@dataclass
class FoundFile:
    id: int
    scan_id: int
    file_type: str
    extension: str
    offset: int
    length: int
    truncated: bool
    recovered_path: Optional[str]
    recovered_at: Optional[float]


class ScanDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def start_scan(self, source_path: str, source_size: int) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO scans (source_path, source_size, started_at, updated_at) VALUES (?, ?, ?, ?)",
            (source_path, source_size, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def resume_scan(self, scan_id: int) -> int:
        """Return the byte offset to resume scanning from."""
        row = self.conn.execute("SELECT bytes_scanned FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such scan: {scan_id}")
        return row["bytes_scanned"]

    def update_progress(self, scan_id: int, bytes_scanned: int):
        self.conn.execute(
            "UPDATE scans SET bytes_scanned = ?, updated_at = ? WHERE id = ?",
            (bytes_scanned, time.time(), scan_id),
        )
        self.conn.commit()

    def finish_scan(self, scan_id: int):
        self.conn.execute("UPDATE scans SET finished_at = ? WHERE id = ?", (time.time(), scan_id))
        self.conn.commit()

    def add_found_file(self, scan_id: int, file_type: str, extension: str, offset: int, length: int, truncated: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO found_files (scan_id, file_type, extension, offset, length, truncated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scan_id, file_type, extension, offset, length, int(truncated)),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_recovered(self, found_id: int, dest_path: str):
        self.conn.execute(
            "UPDATE found_files SET recovered_path = ?, recovered_at = ? WHERE id = ?",
            (dest_path, time.time(), found_id),
        )
        self.conn.commit()

    def list_scans(self):
        return self.conn.execute("SELECT * FROM scans ORDER BY id").fetchall()

    def get_scan(self, scan_id: int):
        return self.conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()

    def list_found_files(self, scan_id: int, file_type: Optional[str] = None) -> Iterator[FoundFile]:
        if file_type:
            rows = self.conn.execute(
                "SELECT * FROM found_files WHERE scan_id = ? AND file_type = ? ORDER BY offset",
                (scan_id, file_type),
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM found_files WHERE scan_id = ? ORDER BY offset", (scan_id,)
            )
        for r in rows:
            yield FoundFile(
                r["id"], r["scan_id"], r["file_type"], r["extension"], r["offset"],
                r["length"], bool(r["truncated"]), r["recovered_path"], r["recovered_at"],
            )

    def get_found_file(self, found_id: int) -> Optional[FoundFile]:
        r = self.conn.execute("SELECT * FROM found_files WHERE id = ?", (found_id,)).fetchone()
        if r is None:
            return None
        return FoundFile(
            r["id"], r["scan_id"], r["file_type"], r["extension"], r["offset"],
            r["length"], bool(r["truncated"]), r["recovered_path"], r["recovered_at"],
        )

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
