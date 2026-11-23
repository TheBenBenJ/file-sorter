"""Google Drive write operations via rclone."""

import os
import subprocess

from sorter.config import DRIVE, DRIVE_RCLONE, nfc


class DriveError(RuntimeError):
    """Raised when an rclone operation on Google Drive fails."""


def rclone(*args):
    try:
        res = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise DriveError(f"rclone {args[0]}: no answer from Google within 5 minutes")
    if res.returncode:
        raise DriveError(f"rclone {args[0]}: {res.stderr.strip()[-300:]}")
    return res.stdout


def drive_unique(folder, name):
    """Pick a non-colliding filename in a Drive folder queried directly via rclone."""
    try:
        taken = {nfc(n.rstrip("/")) for n in rclone("lsf", "--max-depth", "1", DRIVE_RCLONE + folder).splitlines()}
    except DriveError as exc:
        if "directory not found" not in str(exc):
            raise
        taken = set()
    base, ext = os.path.splitext(name)
    idx, candidate = 2, name
    while nfc(candidate) in taken:
        candidate, idx = f"{base} ({idx}){ext}", idx + 1
    return candidate


def drive_move(src, folder, name):
    """Move and rename a file inside Google Drive in one rclone call; returns Drive-relative path."""
    target = f"{folder}/{drive_unique(folder, name)}"
    rclone("moveto", DRIVE_RCLONE + os.path.relpath(src, DRIVE), DRIVE_RCLONE + target)
    return target
