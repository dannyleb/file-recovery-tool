"""Best-effort listing of attached disks, to help the user pick the right
device path for scanning. macOS and Linux only; falls back to a message
telling the user to pass a device path directly."""

import platform
import subprocess
from dataclasses import dataclass
from typing import List


@dataclass
class Disk:
    device: str      # path to pass to `scan`, e.g. /dev/rdisk4 or /dev/sdb
    size_bytes: int
    description: str
    removable: bool


def list_disks() -> List[Disk]:
    system = platform.system()
    if system == "Darwin":
        return _list_disks_macos()
    if system == "Linux":
        return _list_disks_linux()
    raise NotImplementedError(
        f"Automatic disk listing isn't supported on {system}. "
        "Find your drive's device path with your OS's own disk utility and "
        "pass it directly to `filerecovery scan <device>`."
    )


def _list_disks_macos() -> List[Disk]:
    out = subprocess.check_output(["diskutil", "list", "-plist"])
    import plistlib

    plist = plistlib.loads(out)
    disks = []
    for disk_id in plist.get("WholeDisks", []):
        info = plistlib.loads(subprocess.check_output(["diskutil", "info", "-plist", disk_id]))
        size = info.get("TotalSize", 0)
        removable = bool(info.get("RemovableMedia") or info.get("Ejectable"))
        desc = info.get("MediaName", disk_id)
        # Raw (character) device is far faster to read sequentially than the
        # buffered block device on macOS.
        device = f"/dev/r{disk_id}"
        disks.append(Disk(device, size, desc, removable))
    return disks


def _list_disks_linux() -> List[Disk]:
    out = subprocess.check_output(
        ["lsblk", "-b", "-d", "-n", "-o", "NAME,SIZE,MODEL,RM"]
    ).decode()
    disks = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 2:
            continue
        name, size = parts[0], parts[1]
        model = parts[2] if len(parts) > 2 else ""
        removable = len(parts) > 3 and parts[3].strip() == "1"
        disks.append(Disk(f"/dev/{name}", int(size), model or name, removable))
    return disks
