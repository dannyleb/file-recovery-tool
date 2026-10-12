"""Lightweight local HTTP API server for the web GUI.

Run with:
    sudo filerecovery serve --db /path/to/recovery.db --source /dev/rdisk2

The web GUI at https://dannyleb.github.io/file-recovery-tool/ will detect
the server automatically and use it for live data and image previews.
Chrome allows http://localhost from an https page (secure context exception).
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

_INDEX = Path(__file__).parent.parent / "index.html"

from .db import ScanDB
from .scanner import scan as run_scan
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
    # MP4/MOV: f.offset points at the 4-byte box-size field, 4 bytes before the
    # "ftyp" match (see signatures.py header_back_offset=4), so the type tag
    # sits at bytes [4:8].
    "MP4":    lambda b: len(b) >= 8 and b[4:8] == b"ftyp",
}

_MIME = {
    "JPEG": "image/jpeg",
    "PNG":  "image/png",
    "GIF":  "image/gif",
    "BMP":  "image/bmp",
    "PDF":  "application/pdf",
    "MP4":  "video/mp4",
}

_FDA_HINT = (
    "Permission denied. On macOS this usually means your terminal app doesn't "
    "have Full Disk Access: System Settings -> Privacy & Security -> Full Disk "
    "Access -> add your terminal app, then fully quit and relaunch it. sudo "
    "does not bypass this restriction."
)


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, PermissionError) and "Full Disk Access" not in msg:
        return f"{_FDA_HINT} ({msg})"
    return msg


def _real_home() -> str:
    """The invoking user's home directory, not root's.

    When this server runs under sudo, os.path.expanduser('~') resolves
    against root's HOME (often /var/root), silently writing recovered files
    somewhere the actual user can neither find nor read. SUDO_USER names the
    real account, so prefer its home directory when present.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def _expand(path: str) -> str:
    if path == "~" or path.startswith("~/"):
        return _real_home() + path[1:]
    return path


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
            elif path == "/api/scan/start":
                self._start_scan(body)
            elif path == "/api/scan/stop":
                self._stop_scan()
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

        elif path == "/api/disks":
            from .disks import list_disks
            try:
                disks = list_disks()
                self._json([{
                    "device":      d.device,
                    "size_bytes":  d.size_bytes,
                    "description": d.description,
                    "removable":   d.removable,
                } for d in disks])
            except NotImplementedError as e:
                self._json({"error": str(e)}, 501)

        elif path == "/api/scan/active":
            active = bool(self.server.scan_thread and self.server.scan_thread.is_alive())
            self._json({
                "active":  active,
                "scan_id": self.server.scan_active_id,
                "device":  self.server.source_path,
                "error":   self.server.scan_error,
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
        try:
            with Source(src) as source:
                for f in files:
                    try:
                        header = source.read_at(f.offset, 16)
                        if _VALIDATORS[f.file_type](header):
                            valid.append(f.id)
                    except Exception:
                        pass
        except PermissionError as e:
            return self._json({"error": _friendly_error(e)}, 403)

        self._json({"valid": valid})

    def _recover(self, body: dict):
        from .recover import RecoveryError, recover_file
        src = self.server.source_path
        if not src:
            return self._json({"error": "no source configured — restart server with --source"}, 503)

        scan_id = body.get("scan_id")
        ids     = body.get("ids")      # list of ints, or "all"
        dest    = _expand(body.get("dest") or "~/recovered")

        try:
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
        except PermissionError as e:
            return self._json({"error": _friendly_error(e)}, 403)

        self._json({"recovered": results, "errors": errors})

    def _start_scan(self, body: dict):
        device = body.get("device") or self.server.source_path
        if not device:
            return self._json({"error": "no device specified"}, 400)
        if self.server.scan_thread and self.server.scan_thread.is_alive():
            return self._json({"error": "a scan is already running on this server"}, 409)

        resume_id  = body.get("resume_scan_id")
        db_path    = self.server.db_path
        stop_event = threading.Event()
        self.server.scan_stop_event = stop_event
        self.server.scan_error = None

        def run():
            try:
                source = Source(device)
            except Exception as e:
                self.server.scan_error = _friendly_error(e)
                return

            with source, ScanDB(db_path) as db:
                try:
                    if resume_id is not None:
                        scan_id = resume_id
                        start_offset = db.resume_scan(scan_id)
                    else:
                        scan_id = db.start_scan(device, source.size)
                        start_offset = 0
                except Exception as e:
                    self.server.scan_error = _friendly_error(e)
                    return

                self.server.scan_active_id = scan_id
                try:
                    run_scan(source, db, scan_id, start_offset=start_offset,
                             stop_check=lambda: stop_event.is_set())
                except Exception as e:
                    self.server.scan_error = _friendly_error(e)
                    db.log_error(scan_id, e)
                finally:
                    self.server.scan_active_id = None

        self.server.source_path = device
        t = threading.Thread(target=run, daemon=True)
        self.server.scan_thread = t
        t.start()
        self._json({"ok": True, "device": device})

    def _stop_scan(self):
        if self.server.scan_stop_event:
            self.server.scan_stop_event.set()
        self._json({"ok": True})

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

        try:
            with Source(src) as source:
                data = source.read_at(f.offset, limit)
        except PermissionError as e:
            return self._json({"error": _friendly_error(e)}, 403)

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
        self.scan_thread      = None
        self.scan_stop_event  = None
        self.scan_active_id   = None
        self.scan_error       = None
        super().__init__(("127.0.0.1", port), _Handler)


def serve(db_path: str, source_path: Optional[str] = None, port: int = DEFAULT_PORT):
    server = _Server(db_path, source_path, port)
    print(f"File Recovery server → http://localhost:{port}")
    print(f"  DB     : {db_path}")
    print(f"  Source : {source_path or '(not set — pick a drive from the GUI to scan, or restart with --source for previews of an existing scan)'}")
    print(f"  GUI    : https://dannyleb.github.io/file-recovery-tool/")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
