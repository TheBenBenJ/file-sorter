"""Configuration, paths, thresholds, text assets, and core helpers for file-sorter."""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime

from sorter import activity

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(HERE, "settings.json"), encoding="utf-8") as _f:
    SETTINGS = json.load(_f)

MAC = sys.platform == "darwin"
if MAC:
    # Allow launchd jobs to materialize cloud-only files on macOS (VFS_MATERIALIZE_DATALESS_FILES).
    try:
        import ctypes

        ctypes.CDLL("/usr/lib/libSystem.B.dylib").setiopolicy_np(3, 0, 2)
    except OSError, AttributeError:
        pass

ROLE = (
    "decide"
    if "--decide" in sys.argv
    else "apply"
    if "--apply" in sys.argv
    else "deposit"
    if "--deposit" in sys.argv
    else "full"
)

ICLOUD = os.environ.get("FILE_SORTER_ICLOUD") or os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
DRIVE = os.environ.get("FILE_SORTER_DRIVE") or os.path.join(HOME, SETTINGS["drive"])
DATA = os.environ.get("FILE_SORTER_DATA") or HERE
INBOX = os.environ.get("FILE_SORTER_INBOX") or os.path.join(ICLOUD, "00 À trier")
DRIVE_INBOX = os.environ.get("FILE_SORTER_DRIVE_INBOX") or os.path.join(DRIVE, "00 À trier")
DOWNLOADS = os.path.join(HOME, "Downloads")
GMAIL_INBOX = os.path.join(DRIVE, "00 Factures entrantes")
ATTACH_INBOX = os.environ.get("FILE_SORTER_ATTACHMENTS_INBOX") or os.path.join(DRIVE, "00 Pièces jointes")

CHROME_NAMES = ("Enregistré depuis Chrome", "Saved from Chrome")


def chrome_inboxes():
    """Chrome's folders as the Drive shows them (NFC-matched), else the default names."""
    try:
        found = [
            os.path.join(DRIVE, d)
            for d in sorted(os.listdir(DRIVE))
            if unicodedata.normalize("NFC", d) in CHROME_NAMES and os.path.isdir(os.path.join(DRIVE, d))
        ]
    except OSError:
        found = []
    return tuple(found) or tuple(os.path.join(DRIVE, d) for d in CHROME_NAMES)


CHROME_INBOXES = chrome_inboxes()
DRIVE_INBOXES = (DRIVE_INBOX, GMAIL_INBOX, ATTACH_INBOX) + CHROME_INBOXES + (DRIVE,)
DRIVE_RCLONE = os.environ.get("FILE_SORTER_DRIVE_RCLONE")
EXTERNAL = (DOWNLOADS, GMAIL_INBOX) + CHROME_INBOXES + (DRIVE,)

OFFICE = (".docx", ".doc", ".odt", ".xlsx", ".xls", ".ods", ".pptx", ".ppt", ".odp")
DOCUMENTS = OFFICE + (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".rtf",
    ".pages",
    ".numbers",
    ".key",
    ".txt",
    ".csv",
    ".md",
)
ROOT_DOCUMENTS = OFFICE + (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".rtf",
    ".pages",
    ".numbers",
    ".key",
)

PREVIEWS = tempfile.mkdtemp(prefix="file-sorter-")
atexit.register(shutil.rmtree, PREVIEWS, ignore_errors=True)

PARTIAL_EXT = (".crdownload", ".download", ".part", ".partial", ".tmp", ".opdownload")
MAX_SIZE = int(os.environ.get("FILE_SORTER_DOWNLOADS_MAX_MB", "50")) * 1024 * 1024
LOG = os.path.join(HOME, "Library/Logs/file-sorter.log") if MAC else None
LOCK = os.path.join(DATA, "sort.lock")
KEPT = os.path.join(DATA, "kept.json")
PLAN = os.path.join(DATA, "plan.json")
RESULTS = os.path.join(DATA, "results.json")
APPLIED = os.path.join(DATA, "applied.json")


def _default_agy():
    for candidate in ("/usr/local/bin/agy", os.path.join(HOME, ".local/bin/agy")):
        if os.path.isfile(candidate):
            return candidate
    return "/usr/local/bin/agy"


ENGINE = (os.environ.get("FILE_SORTER_ENGINE") or "antigravity").strip().lower()
if ENGINE not in ("antigravity", "claude"):
    ENGINE = "antigravity"
AGY = os.environ.get("FILE_SORTER_AGY") or _default_agy()
AGY_MODEL = os.environ.get("FILE_SORTER_AGY_MODEL") or "gemini-3.8-flash-medium"
CLAUDE = os.environ.get("FILE_SORTER_CLAUDE") or "/opt/homebrew/bin/claude"
MODEL = os.environ.get("FILE_SORTER_MODEL", "haiku")
LOCAL_URL = os.environ.get("FILE_SORTER_LOCAL_URL", "http://127.0.0.1:11234")
LOCAL_MODEL = "qwen3.5-9b"
SHADOW = os.environ.get("FILE_SORTER_SHADOW") == "1"
LOCAL_CONTEXT, LOCAL_MARGIN, LOCAL_ANSWER, LOCAL_FILES, LOCAL_TIMEOUT = 16384, 256, 400, 5, 300
AGY_AI = {"engine": "antigravity", "model": AGY_MODEL}
CLAUDE_AI = {"engine": "claude", "model": MODEL}
CLOUD_AI = AGY_AI if ENGINE == "antigravity" else CLAUDE_AI
LOCAL_AI = {"engine": "local", "model": LOCAL_MODEL}

MIN_AGE = int(os.environ.get("FILE_SORTER_MIN_AGE", "60"))
MAX_FILES = 15
THRESHOLD = 0.7
DRY = "--dry-run" in sys.argv

CLEANUP = {"on": "on", "off": "off"}.get(os.environ.get("FILE_SORTER_CLEANUP", ""), "trial")
ARCHIVES = SETTINGS.get("archives") or "90 Archives"
ARCHIVE_NAME = re.compile(r"\d\d Archives?", re.I)
THEME = re.compile(r"\d\d ")
REVIEW = os.path.join(DATA, "review.json")
REVIEW_KINDS = ("archiver", "supprimer")
DECISIONS = ("garder", "archiver", "supprimer")
REVIEW_LEAVE, REVIEW_TRASH = 0.7, 0.9
REVIEW_NO_TRASH = ("icloud", "drive") if os.environ.get("FILE_SORTER_NO_TRASH") == "1" else ("drive",)


def kept_from_trash(root):
    if root == "drive":
        return "rien n'est supprimé dans le Drive"
    return "rien n'est supprimé, par précaution"


REVIEW_RULES = 7
UNREAD_FORMAT = "format que la relecture ne lit pas"
REVIEW_BILAN = "2026-11-06"
REVIEW_BILAN_LINE = int(os.environ.get("FILE_SORTER_REVIEW_BILAN_LINE") or 16000)
REVIEW_EXT = OFFICE + (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".rtf",
    ".pages",
    ".numbers",
    ".key",
    ".txt",
)
REVIEW_VISUAL = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif")
REVIEW_FILES = 10
REVIEW_PASS = max(1, int(os.environ.get("FILE_SORTER_REVIEW_PASS") or 120))
REVIEW_PASS_TIME = 270
REVIEW_APPLY = 40
REVIEW_CHARS = 1200
REVIEW_LISTING = 150
REVIEW_MIN_AGE = 3 * 3600
REVIEW_AGAIN = 180 * 86400
REVIEW_RETRIES = (3600, 86400, 30 * 86400)
REVIEW_REST = 3600
REVIEW_PAUSE = 3600

STEPS = ("inboxes", "decide", "drive", "plan", "icloud")
if ROLE == "decide":
    activity.PATH = os.path.join(DATA, "activity.jsonl")
REPORT = activity.Report(os.path.join(DATA, "run-status.json") if ROLE == "decide" else None, STEPS)
UNREADABLE = []

TEXT_EXT = {".txt", ".csv", ".ics", ".md", ".json", ".html", ".htm", ".xml"}
ZIP_OFFICE_EXT = {".docx", ".pptx", ".xlsx", ".docm", ".xlsm", ".odt", ".ods", ".odp"}
TEXTUTIL_EXT = {".doc", ".rtf", ".pages"}
VISION_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}

with open(os.path.join(HERE, "convention.md"), encoding="utf-8") as _f:
    CONVENTION = _f.read()
with open(os.path.join(HERE, "rules.md"), encoding="utf-8") as _f:
    RULES = _f.read().strip()
_review_owner_path = os.path.join(HERE, "review-rules.md")
if os.path.isfile(_review_owner_path):
    with open(_review_owner_path, encoding="utf-8") as _f:
        REVIEW_OWNER = _f.read().strip()
else:
    REVIEW_OWNER = ""


def nfc(text):
    return unicodedata.normalize("NFC", text)


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line, flush=True)


def notify(msg):
    if not MAC:
        subprocess.run(["notifier", "Tri des fichiers", msg], check=False, capture_output=True)
        return
    subprocess.run(
        [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            'display notification (item 1 of argv) with title "Tri iCloud"',
            "-e",
            "end run",
            msg,
        ],
        check=False,
    )


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        return default


def write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def is_archive(top):
    """Return True if a top-level folder is the archive folder (e.g. '90 Archives')."""
    return bool(ARCHIVE_NAME.fullmatch(nfc(top))) or nfc(top) == nfc(ARCHIVES)


def archive_of(base):
    """Return the existing top-level 'NN Archives' folder name under base, or ARCHIVES."""
    try:
        found = sorted(
            d for d in os.listdir(base) if ARCHIVE_NAME.fullmatch(nfc(d)) and os.path.isdir(os.path.join(base, d))
        )
    except OSError:
        found = []
    return found[0] if found else ARCHIVES


def folders():
    """Return destination folders in iCloud (up to 4 levels deep, excluding 00 inboxes and archives)."""
    out = []
    for root, dirs, _ in os.walk(ICLOUD):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel = nfc(os.path.relpath(root, ICLOUD))
        if rel == ".":
            dirs[:] = [d for d in dirs if not nfc(d).startswith("00 ") and not is_archive(d)]
            continue
        if rel.count(os.sep) < 4:
            out.append(rel)
    return out
