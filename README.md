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

## How it works

1. **Scan** — reads the device (or a disk image) from start to end, looking
   for file headers. For every match, it figures out the file's length and
   records `(type, offset, length)` in a small SQLite database. Nothing is
   written to the source drive during a scan; it's opened read-only throughout.
2. **List** — browse what the scan found before committing to anything.
3. **Recover** — copies the exact byte range for the files you choose out to
   a destination of your choice.

Supported types: JPEG, PNG, GIF, PDF, ZIP (and anything ZIP-based — docx,
xlsx, pptx, jar, apk), BMP, WAV, AVI, SQLite databases, and MP4/MOV.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `filerecovery` command into your virtualenv.

## Usage

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
sudo filerecovery scan /dev/rdisk2 --db recovery.db
```

Raw disk access needs root. Scanning is read-only and will not modify the
drive. Progress is saved as it goes, so if you need to stop, `Ctrl-C` and
resume later:

```bash
sudo filerecovery scan /dev/rdisk2 --db recovery.db --resume 1
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

- **No original filenames, folder structure, or timestamps.** Carving finds
  file *content*, not filesystem metadata — recovered files are named by
  type and the byte offset they were found at (e.g. `jpeg_000005206000.jpg`).
- **A few formats are exact, most are best-effort.** PNG, GIF, MP4/MOV, WAV,
  AVI, BMP, ZIP, and SQLite are bounded by parsing the format's own internal
  structure, so their recovered length is exact when that structure is
  intact. JPEG and PDF are found by searching for a known footer, which is
  reliable in practice but not a hard guarantee. A file flagged `TRUNCATED`
  means no footer/terminator was found and the tool gave up at a size cap —
  the recovered bytes are likely incomplete or include trailing garbage.
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
