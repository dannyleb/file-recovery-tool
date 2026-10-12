# file-recovery-tool

Recovers permanently deleted files from a hard drive by scanning it directly
for file signatures ("carving"), independent of the filesystem. Because it
doesn't rely on filesystem metadata (which is usually the first thing to be
overwritten or lost when a file is deleted), it can find files even after
the original directory entry is gone, or the drive was reformatted.

This is the same technique used by tools like PhotoRec and Scalpel: scan the
raw bytes for the "magic numbers" that mark the start of a known file type,
then work out exactly how long that file is by parsing the format's own
internal structure (or, when that's not possible, by capping at a sane
maximum size).

## Quickstart (macOS, from scratch)

```bash
# 1. Get the code
git clone https://github.com/dannyleb/file-recovery-tool
cd file-recovery-tool

# 2. Install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. Confirm the install and find your drive
which filerecovery
filerecovery list-disks
```

`list-disks` prints each attached drive's device path — note the one for
the drive you want to recover from (e.g. `/dev/rdisk2`) and double-check its
size against Finder/Disk Utility before continuing, since the next step
reads the entire thing.

```bash
# 4. Start the local server (needs root for raw disk access)
sudo "$(which filerecovery)" serve --db ~/recovery.db --port 7523
```

Use `sudo "$(which filerecovery)" ...`, not just `sudo filerecovery ...` —
`sudo` resets your `PATH`, so it can't see the copy of `filerecovery`
installed inside your virtualenv's `bin/` folder. Resolving the full path
first with `which` and handing that to `sudo` sidesteps it. If you still get
`command not found`, your venv probably isn't active in this terminal —
run `source .venv/bin/activate` again and re-check with `which filerecovery`
before retrying.

Leave this terminal open; it's your server. You should see:
```
File Recovery server → http://localhost:7523
  DB     : /Users/you/recovery.db
  ...
Press Ctrl+C to stop.
```

Now, in your browser:

5. Go to **https://dannyleb.github.io/file-recovery-tool/**
6. Click **Open DB…** and select the `recovery.db` file from step 4
7. Click **Scan Disk…**, pick your drive from the list, confirm — progress
   shows live, right in the browser, no second terminal needed
8. Once files show up, select some (or **Recover All**) and click
   **Recover Selected**

**If you hit a permission error anywhere in steps 4–8** on macOS, it's
almost always **Full Disk Access**, not a bug: go to **System Settings →
Privacy & Security → Full Disk Access**, add your terminal app (Terminal.app
or iTerm), then fully quit and reopen that terminal before retrying. `sudo`
does **not** bypass this — root privileges and Full Disk Access are two
separate permission systems on modern macOS, and raw disk reads (plus
writes to protected folders like Desktop/Documents/Downloads) need the
latter regardless of UID.

## Architecture

```
filerecovery/
  signatures.py   Per-type header + extent-parsing logic (the carving rules)
  source.py       Read-only, sector-aligned access to a device/image, with caching
  scanner.py      Sequential scan loop: finds headers, computes extents, writes to db
  db.py           SQLite storage for scans, found files, and errors
  recover.py      Copies a found file's byte range to a destination, with the
                  same-disk safety check
  disks.py        Best-effort disk listing (macOS/Linux) for `list-disks`
  metadata.py     Extracts embedded dates (EXIF/PNG/MP4) and thumbnails from
                  carved bytes — exception-safe, falls back to None
  server.py       Local HTTP API (stdlib only) that backs the web GUI
  gui.py          Legacy PyQt6 desktop GUI (superseded by the web GUI below;
                  still installable but not actively developed)
cli.py             argparse entry point: list-disks / scan / list / recover / serve
index.html         Web GUI, served from GitHub Pages and from the local server
```

**Data flow:** `scan` reads the source sequentially in 16 MB chunks, searches
each chunk for every signature's header bytes, and for each match calls that
signature's `extent_fn` to work out the exact length (parsing the format's
own internal structure where possible, or searching for a footer / capping at
a max size otherwise). Every find is written to SQLite immediately — nothing
is buffered in memory and progress is checkpointed every 2 seconds, so a scan
of a multi-terabyte drive can be interrupted and resumed. `recover` later
reads just the `(offset, length)` byte range back out and writes it to a
destination file.

### Supported types & how each one is bounded

| Type | Header | Extent method | Exact? |
|---|---|---|---|
| JPEG | `FF D8 FF` | search for `FF D9` end marker | best-effort |
| PNG | `89 50 4E 47 0D 0A 1A 0A` | walk chunks to `IEND` | exact |
| GIF | `GIF87a` / `GIF89a` | walk blocks to trailer `0x3B` | exact |
| PDF | `%PDF-` | search for last `%%EOF` | best-effort |
| ZIP (+ docx/xlsx/pptx/jar/apk) | `PK\x03\x04` | find End-Of-Central-Directory record | best-effort |
| WAV | `RIFF`...`WAVE` | RIFF chunk-size field | exact |
| AVI | `RIFF`...`AVI ` | RIFF chunk-size field | exact |
| BMP | `BM` | cross-checked header fields (high false-positive rate otherwise) | exact |
| SQLite | `SQLite format 3\0` | page size × page count from header | exact |
| MP4/MOV | `ftyp` | walk top-level boxes | exact |

A file is flagged `TRUNCATED` when its extent function ran out of data or
hit the format's max-size cap without finding a real end marker — the
recovered bytes may be incomplete.

### Read layer (`source.py`)

Raw character devices on macOS reject single reads over ~8 MB (`EINVAL`) and
require sector-aligned (512-byte) offsets, so every read is split into
sector-aligned ≤8 MB chunks and re-stitched. A single-slot cache keeps the
last large read in memory so that dense clusters of nearby lookups (e.g. the
extent search for dozens of JPEGs packed close together) hit memory instead
of re-reading the device — this was added because scans were stalling on
signature clusters without it.

### Database (`db.py`)

SQLite, WAL mode, 10s busy timeout (so the GUI's background polling doesn't
collide with a running scan's writes). Three tables: `scans` (progress
checkpoint), `found_files` (one row per carved file), `scan_errors`. Schema
is created on first connect; `ALTER TABLE` migrations for older DBs run
automatically and silently no-op if the column already exists.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `filerecovery` CLI (`list-disks`, `scan`, `list`,
`recover`, `serve`) into your virtualenv. Date/thumbnail extraction needs
Pillow; install it with:

```bash
pip install -e ".[gui]"   # also pulls in PyQt6 for the legacy desktop GUI
```

## Running the GUI (current, recommended path)

The GUI is a static web page — hosted on GitHub Pages at
**https://dannyleb.github.io/file-recovery-tool/** — that talks to a small
local server for anything that needs real disk access (scanning, previews,
recovery). The page itself never leaves your machine; GitHub Pages just
serves the HTML/JS. See the Quickstart above for the full from-scratch
walkthrough; this section covers the pieces in more depth.

**1. Start the local server** (needs root for raw disk access):

```bash
sudo "$(which filerecovery)" serve --db recovery.db --port 7523
```

- `--db` — path to the scan database (created if it doesn't exist; reuse the
  same file to resume browsing a previous scan)
- `--source` — optional. The device or disk image to read for previews and
  recovery. You normally don't need this anymore — pick a drive from the
  **Scan Disk…** button in the GUI instead, and the server adopts whatever
  you pick. Pass it explicitly only if you want to browse/recover an
  existing scan's results without starting a new one.
- `--port` — defaults to `7523`

**2. Open the GUI** — go to https://dannyleb.github.io/file-recovery-tool/
in your browser. On load it probes `http://localhost:7523/api/status`
automatically; once it gets a response the server-dependent controls
(scanning, previews, recover-without-terminal, filters) light up.
Chrome/Safari both allow an `https://` page to call `http://localhost` —
that's a built-in "secure context" exception, not something you need to
configure.

If you'd rather not depend on GitHub Pages being reachable, the same page is
served directly by the local server too — `http://localhost:7523/` renders
identically and needs no internet access.

**3. Open a database, then scan** — **Open DB…** to create/open a
`recovery.db` (the File System Access API, so Chrome or Edge), then
**Scan Disk…** to list attached drives (via the server) and start a scan on
whichever one you pick, running in a background thread on the server — no
second terminal. A **Stop Scan** button appears in the toolbar while a scan
is running (whether it was started from the GUI or from `filerecovery scan`
in a terminal — both check the same stop flag).

**4. Browse and recover** — pick a scan from the sidebar (most recent is
auto-selected), watch progress live, browse/filter found files (type,
"Has Date", "Previews Only", "Hide Truncated"), arrow-key through the list
with live image/video preview, and select rows → **Recover** to copy them
out through the `/api/recover` endpoint, no terminal needed.

### API reference (`filerecovery/server.py`)

All endpoints are plain JSON over HTTP on `127.0.0.1`, CORS-open (`*`), no
auth — it's meant to be reachable only from your own browser on the same
machine.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | `{ok, db, source}` — used by the GUI to detect the server |
| GET | `/api/disks` | Attached drives (device, size, description, removable) for the Scan Disk picker |
| POST | `/api/scan/start` | `{device}` → starts a scan in a background thread on the server |
| GET | `/api/scan/active` | `{active, scan_id, device, error}` — status of a server-started scan |
| POST | `/api/scans/<id>/stop` | Requests a running scan (GUI- or CLI-started) stop at the next chunk boundary |
| GET | `/api/scans` | List all scans in the DB |
| GET | `/api/scans/<id>` | One scan's progress/metadata |
| GET | `/api/scans/<id>/files?type=JPEG` | Found files for a scan, optional type filter |
| GET | `/api/scans/<id>/valid-previews` | Re-validates each image/video file's header against the live source; returns the ids that are genuinely renderable |
| GET | `/api/errors/<scan_id>` | Last 200 scan errors |
| GET | `/api/preview/<found_id>` | Raw bytes (capped at 10 MB) for inline image/video preview |
| POST | `/api/recover` | `{scan_id, ids: [...] or "all", dest}` → copies files, returns `{recovered, errors}` |
| GET | `/` | Serves `index.html` itself |

A `--source` must be set (explicitly at startup, or implicitly by starting a
scan via `/api/scan/start`) for anything that reads the drive — previews,
recover, valid-previews — without it those return `503`. `recover`'s `dest`
defaults to `~/recovered`, resolved against the *invoking* user's home
directory even when the server is running under `sudo` (via `SUDO_USER`) —
not root's.

## Legacy desktop GUI (PyQt6)

A native desktop GUI (`filerecovery-gui`) exists from before the pivot to
the web GUI above. It still works but isn't being actively developed —
the web GUI superseded it to sidestep PyQt6 rendering/Full-Disk-Access
quirks on macOS. Launch with:

```bash
filerecovery-gui        # or: sudo "$(which filerecovery-gui)" for raw disk access
```

Workflow: **Open/New DB** → select a drive under *Devices* → **Scan Disk…**
→ browse results with thumbnails/dates in the right panel → select rows →
**Recover Selected** / **Recover All**. A **Destination** picker in the
toolbar lets you set the recovery folder once and reuse it across
recoveries instead of being asked every time.

## CLI usage (works standalone, no GUI/server needed)

**1. Find your drive's device path.**

```bash
filerecovery list-disks
```

```
/dev/rdisk2   4.5TB   removable   Elements 2620
```

Use the `r`-prefixed (raw/character) device on macOS — it's much faster to
read sequentially than the buffered block device. Double-check the size and
description against what you see in Disk Utility / Finder before you
proceed — the next command reads the *entire* drive.

**2. Scan it.**

```bash
sudo "$(which filerecovery)" scan /dev/rdisk2 --db recovery.db
```

(Use the resolved-path form, not plain `sudo filerecovery ...` — `sudo`
resets `PATH` and won't see a `filerecovery` installed inside a virtualenv.
See the Quickstart above if that's unfamiliar.)

Raw disk access needs root. Scanning is read-only and will not modify the
drive. Progress is saved as it goes, so if you need to stop, `Ctrl-C` and
resume later:

```bash
sudo "$(which filerecovery)" scan /dev/rdisk2 --db recovery.db --resume 1
```

A full scan of a large drive can take hours — it's limited by how fast the
drive can be read sequentially, not by CPU.

**3. See what it found.**

```bash
filerecovery list --db recovery.db          # list scans
filerecovery list 1 --db recovery.db        # list files found by scan 1
filerecovery list 1 --db recovery.db --type JPEG
```

Each entry shows its type, offset, size, and whether it's flagged
`TRUNCATED` (see Limitations below).

**4. Recover the ones you want.**

```bash
filerecovery recover 1 3 7 12 --db recovery.db --dest ~/recovered
filerecovery recover 1 all --db recovery.db --dest /Volumes/OtherDrive/recovered
```

**The destination must not be on the drive you're recovering from.** The
tool checks this and refuses if it looks like the same disk, but treat that
check as a backstop, not a guarantee — recover to a *different physical
drive* whenever you can. Writing recovered files back onto the same disk you're
scanning can overwrite other deleted data you haven't recovered yet.

## Limitations

This tool only does signature-based carving. That means:

- **No original filenames, folder structure, or timestamps from the
  filesystem.** Carving finds file *content*, not filesystem metadata —
  recovered files are named by type and the byte offset they were found at
  (e.g. `jpeg_000005206000.jpg`). Some formats carry their own embedded date
  (JPEG EXIF, PNG tEXt, MP4 `mvhd`) and that's extracted separately — see
  `metadata.py` — but there's no original filename anywhere to recover from.
- **A few formats are exact, most are best-effort** — see the table above.
  JPEG, PDF, and ZIP are footer/record-search based, which is reliable in
  practice but not a hard guarantee. A file flagged `TRUNCATED` means no
  footer/terminator was found and the tool gave up at a size cap — the
  recovered bytes are likely incomplete or include trailing garbage.
- **Heavily fragmented files will not recover correctly.** If a file's data
  was split into non-contiguous pieces on disk (common on a nearly-full or
  heavily-used drive), carving only recovers the first contiguous chunk.
- **Overwritten data is gone.** If new data has been written to the sectors
  a deleted file occupied, no software can recover it. The sooner you stop
  using a drive after deleting something and run a recovery scan, the better
  your odds.
- **BMP in particular has a high false-positive rate.** Its 2-byte header
  (`BM`) turns up by chance in random data; the tool cross-checks several
  internal header fields to filter most of these out, but some junk matches
  may still slip through.

## A note on safety

- The tool never writes to the source drive. If you want extra assurance,
  use a hardware write-blocker, or scan a disk image (`dd`/`ddrescue`
  output) instead of the live device.
- If the drive is failing (clicking, disconnecting, SMART errors), image it
  first with a tool built for that (e.g. `ddrescue`) rather than scanning it
  directly — this tool does one straightforward sequential read with no
  retry/bad-sector handling.

## Development

```bash
pip install -e . pytest
pytest
```

Tests build small synthetic "disk images" with known files embedded in
random junk bytes (including one deliberately straddling a chunk boundary)
and assert the scanner finds them at the right offset/length and that
recovery reproduces them byte-for-byte.
