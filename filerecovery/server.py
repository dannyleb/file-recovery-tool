"""Lightweight local HTTP API server for the web GUI.

Run with:
    sudo filerecovery serve --db /path/to/recovery.db --source /dev/rdisk2

The web GUI at https://dannyleb.github.io/file-recovery-tool/ will detect
the server automatically and use it for live data and image previews.
Chrome allows http://localhost from an https page (secure context exception).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

_INDEX = Path(__file__).parent.parent / "index.html"

from .db import ScanDB
from .source import Source

DEFAULT_PORT = 7523
PREVIEW_CAP  = 10 * 1024 * 1024  # 10 MB max sent for preview

# Strict per-type header validators — read 16 bytes, return True if genuinely valid
_VALIDATORS = {
    "JPEG":   lambda b: len(b) >= 4 and b[:3] == b"\xff\xd8\xff" and b[3] in {
                  0xe0,0xe1,0xe2,0xe3,0xe4,0xe5,0xe6,0xe7,
                  0xe8,0xe9,0xea,0xeb,0xec,0xed,0xee,0xef,
                  0xdb,0xc0,0xc1,0xc2,0xc3,0xc4,0xc5,0xc6,
              },
    "PNG":    lambda b: b[:8] == b"\x89PNG\r\n\x1a\n",
    "GIF":    lambda b: b[:6] in (b"GIF87a", b"GIF89a"),
    "BMP":    lambda b: len(b) >= 6 and b[:2] == b"BM" and int.from_bytes(b[2:6], "little") > 54,
}

_MIME = {
    "JPEG": "image/jpeg",
    "PNG":  "image/png",
    "GIF":  "image/gif",
    "BMP":  "image/bmp",
    "PDF":  "application/pdf",
}


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, *_):
        pass  # silence default request logging

    # ------------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)
        try:
            self._route(path, qs)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/recover":
                self._recover(body)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    # ------------------------------------------------------------------
    def _route(self, path: str, qs: dict):
        if path == "/api/status":
            self._json({
                "ok":     True,
                "db":     self.server.db_path,
                "source": self.server.source_path,
            })

        elif path == "/api/scans":
            with ScanDB(self.server.db_path) as db:
                self._json([dict(s) for s in db.list_scans()])

        elif path.startswith("/api/scans/"):
            parts = path.split("/")            # ['', 'api', 'scans', '<id>', ...]
            scan_id = int(parts[3])
            if len(parts) >= 5 and parts[4] == "files":
                ft = qs.get("type", [None])[0]
                with ScanDB(self.server.db_path) as db:
                    files = list(db.list_found_files(scan_id, file_type=ft))
                self._json([{
                    "id":             f.id,
                    "scan_id":        f.scan_id,
                    "file_type":      f.file_type,
                    "extension":      f.extension,
                    "offset":         f.offset,
                    "length":         f.length,
                    "truncated":      f.truncated,
                    "file_date":      f.file_date,
                    "recovered_path": f.recovered_path,
                } for f in files])
            elif len(parts) >= 5 and parts[4] == "valid-previews":
                self._valid_previews(scan_id)
            else:
                with ScanDB(self.server.db_path) as db:
                    row = db.get_scan(scan_id)
                if row is None:
                    return self._json({"error": "not found"}, 404)
                self._json(dict(row))

        elif path.startswith("/api/errors/"):
            scan_id = int(path.split("/")[3])
            with ScanDB(self.server.db_path) as db:
                try:
                    rows = db.conn.execute(
                        "SELECT occurred_at, error_type, message, offset "
                        "FROM scan_errors WHERE scan_id=? ORDER BY occurred_at DESC LIMIT 200",
                        (scan_id,)
                    ).fetchall()
                    self._json([dict(r) for r in rows])
                except Exception:
                    self._json([])

        elif path.startswith("/api/preview/"):
            found_id = int(path.split("/")[3])
            self._serve_preview(found_id)

        elif path in ("", "/", "/index.html"):
            self._serve_index()

        else:
            self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------
    def _serve_index(self):
        try:
            body = _INDEX.read_bytes()
        except OSError:
            return self._json({"error": "index.html not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _valid_previews(self, scan_id: int):
        src = self.server.source_path
        if not src:
            return self._json({"error": "no source configured"}, 503)

        with ScanDB(self.server.db_path) as db:
            files = [f for f in db.list_found_files(scan_id) if f.file_type in _VALIDATORS]

        valid = []
        with Source(src) as source:
            for f in files:
                try:
                    header = source.read_at(f.offset, 16)
                    if _VALIDATORS[f.file_type](header):
                        valid.append(f.id)
                except Exception:
                    pass

        self._json({"valid": valid})

    def _recover(self, body: dict):
        from .recover import RecoveryError, recover_file
        src = self.server.source_path
        if not src:
            return self._json({"error": "no source configured — restart server with --source"}, 503)

        scan_id = body.get("scan_id")
        ids     = body.get("ids")      # list of ints, or "all"
        dest    = body.get("dest", os.path.expanduser("~/recovered"))
        dest    = os.path.expanduser(dest)

        with ScanDB(self.server.db_path) as db:
            if ids == "all":
                targets = list(db.list_found_files(scan_id))
            else:
                targets = [db.get_found_file(int(i)) for i in ids]
                targets = [f for f in targets if f is not None]

            results = []
            errors  = []
            with Source(src) as source:
                for f in targets:
                    try:
                        path = recover_file(source, db, f, dest)
                        results.append({"id": f.id, "path": path})
                    except RecoveryError as e:
                        errors.append({"id": f.id, "error": str(e)})

        self._json({"recovered": results, "errors": errors})

    def _serve_preview(self, found_id: int):
        src = self.server.source_path
        if not src:
            return self._json({"error": "no source configured"}, 503)

        with ScanDB(self.server.db_path) as db:
            f = db.get_found_file(found_id)
        if f is None:
            return self._json({"error": "not found"}, 404)

        mime  = _MIME.get(f.file_type, "application/octet-stream")
        limit = min(f.length, PREVIEW_CAP)

        with Source(src) as source:
            data = source.read_at(f.offset, limit)

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type",   mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    def _json(self, data, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


class _Server(HTTPServer):
    def __init__(self, db_path: str, source_path: Optional[str], port: int):
        self.db_path     = db_path
        self.source_path = source_path
        super().__init__(("127.0.0.1", port), _Handler)


def serve(db_path: str, source_path: Optional[str] = None, port: int = DEFAULT_PORT):
    server = _Server(db_path, source_path, port)
    print(f"File Recovery server → http://localhost:{port}")
    print(f"  DB     : {db_path}")
    print(f"  Source : {source_path or '(not set — previews disabled)'}")
    print(f"  GUI    : https://dannyleb.github.io/file-recovery-tool/")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
