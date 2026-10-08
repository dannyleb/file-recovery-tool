import argparse
import os
import sys

from .db import ScanDB
from .recover import RecoveryError, recover_file
from .scanner import scan as run_scan
from .source import Source


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def cmd_list_disks(args):
    from .disks import list_disks

    try:
        disks = list_disks()
    except NotImplementedError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not disks:
        print("No disks found.")
        return 0
    for d in disks:
        tag = "removable" if d.removable else "fixed"
        print(f"{d.device}\t{_fmt_size(d.size_bytes)}\t{tag}\t{d.description}")
    return 0


def cmd_scan(args):
    if not os.path.exists(args.device):
        print(f"No such path: {args.device}", file=sys.stderr)
        return 1

    try:
        source = Source(args.device)
    except PermissionError:
        print(
            f"Permission denied opening {args.device}. Raw disk access usually needs "
            f"sudo (and, on macOS, Full Disk Access for your terminal). Try:\n"
            f"  sudo filerecovery scan {args.device} --db {args.db}",
            file=sys.stderr,
        )
        return 1

    with source, ScanDB(args.db) as db:
        if args.resume is not None:
            scan_id = args.resume
            start_offset = db.resume_scan(scan_id)
            print(f"Resuming scan {scan_id} at {_fmt_size(start_offset)} / {_fmt_size(source.size)}")
        else:
            scan_id = db.start_scan(args.device, source.size)
            start_offset = 0
            print(f"Started scan {scan_id} on {args.device} ({_fmt_size(source.size)})")

        def progress(done, total):
            pct = (done / total * 100) if total else 100
            print(f"\r  {_fmt_size(done)} / {_fmt_size(total)} ({pct:.1f}%)", end="", flush=True)

        try:
            found = run_scan(source, db, scan_id, start_offset=start_offset, progress_cb=progress)
        except KeyboardInterrupt:
            print(f"\nInterrupted. Resume with: filerecovery scan {args.device} --db {args.db} --resume {scan_id}")
            return 130
        print(f"\nDone. Found {found} file(s) this run. Browse with: filerecovery list --db {args.db} {scan_id}")
        return 0


def cmd_list(args):
    with ScanDB(args.db) as db:
        if args.scan_id is None:
            scans = db.list_scans()
            if not scans:
                print("No scans yet.")
                return 0
            for s in scans:
                status = "finished" if s["finished_at"] else "in progress"
                print(
                    f"scan {s['id']}: {s['source_path']} "
                    f"({_fmt_size(s['bytes_scanned'])} / {_fmt_size(s['source_size'])}, {status})"
                )
            return 0

        files = list(db.list_found_files(args.scan_id, file_type=args.type))
        if not files:
            print("No files found (yet) for this scan.")
            return 0
        for f in files:
            flags = []
            if f.truncated:
                flags.append("TRUNCATED")
            if f.recovered_path:
                flags.append(f"recovered -> {f.recovered_path}")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"{f.id}\t{f.file_type}\toffset={f.offset}\tsize={_fmt_size(f.length)}{flag_str}")
        return 0


def cmd_recover(args):
    with ScanDB(args.db) as db:
        scan_row = db.get_scan(args.scan_id)
        if scan_row is None:
            print(f"No such scan: {args.scan_id}", file=sys.stderr)
            return 1

        if args.ids == ["all"]:
            targets = list(db.list_found_files(args.scan_id))
        else:
            targets = []
            for raw_id in args.ids:
                f = db.get_found_file(int(raw_id))
                if f is None or f.scan_id != args.scan_id:
                    print(f"No found-file {raw_id} in scan {args.scan_id}", file=sys.stderr)
                    return 1
                targets.append(f)

        with Source(scan_row["source_path"]) as source:
            recovered = 0
            for f in targets:
                try:
                    dest = recover_file(source, db, f, args.dest)
                except RecoveryError as e:
                    print(f"Refusing to recover file {f.id}: {e}", file=sys.stderr)
                    return 1
                print(f"Recovered {f.id} ({f.file_type}, {_fmt_size(f.length)}) -> {dest}")
                recovered += 1
        print(f"Recovered {recovered} file(s) to {args.dest}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="filerecovery",
        description="Find and restore permanently deleted files from a disk by scanning it for file signatures.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_disks = sub.add_parser("list-disks", help="List attached disks and their device paths")
    p_disks.set_defaults(func=cmd_list_disks)

    p_scan = sub.add_parser("scan", help="Scan a device or disk image for recoverable files")
    p_scan.add_argument("device", help="Device path (e.g. /dev/rdisk4) or disk-image file")
    p_scan.add_argument("--db", required=True, help="Path to the scan database (created if missing)")
    p_scan.add_argument("--resume", type=int, default=None, help="Resume an interrupted scan by its id")
    p_scan.set_defaults(func=cmd_scan)

    p_list = sub.add_parser("list", help="List scans, or the files found by one")
    p_list.add_argument("scan_id", type=int, nargs="?", help="Scan id (omit to list all scans)")
    p_list.add_argument("--db", required=True)
    p_list.add_argument("--type", help="Filter by file type, e.g. JPEG")
    p_list.set_defaults(func=cmd_list)

    p_recover = sub.add_parser("recover", help="Write found files out to a destination directory")
    p_recover.add_argument("scan_id", type=int)
    p_recover.add_argument("ids", nargs="+", help="Found-file ids to recover, or 'all'")
    p_recover.add_argument("--db", required=True)
    p_recover.add_argument("--dest", required=True, help="Destination directory (must NOT be on the source disk)")
    p_recover.set_defaults(func=cmd_recover)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
