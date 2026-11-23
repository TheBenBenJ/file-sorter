"""Filesystem helpers: inbox discovery, text extraction, deduplication, safe paths, and plan actions."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

from sorter.config import (
    CHROME_INBOXES,
    DOCUMENTS,
    DOWNLOADS,
    DRIVE,
    DRY,
    HOME,
    ICLOUD,
    KEPT,
    MAC,
    MAX_FILES,
    MAX_SIZE,
    MIN_AGE,
    PARTIAL_EXT,
    ROLE,
    ROOT_DOCUMENTS,
    TEXT_EXT,
    TEXTUTIL_EXT,
    UNREADABLE,
    ZIP_OFFICE_EXT,
    log,
    nfc,
    notify,
)

TOO_BIG = set()


def relative(path):
    """Return (root, nfc-relative-path) for a path inside Google Drive or iCloud."""
    if path == DRIVE or path.startswith(DRIVE + os.sep):
        return ("drive", nfc(os.path.relpath(path, DRIVE)))
    return ("icloud", nfc(os.path.relpath(path, ICLOUD)))


def place(root, rel=""):
    """Human-readable location label ('Google Drive/...' or 'iCloud/...')."""
    prefix = "Google Drive" if root == "drive" else "iCloud"
    return f"{prefix}/{'' if rel == '.' else rel}".rstrip("/")


def where(path):
    """Human-readable location label for a full path in iCloud or Google Drive."""
    return place(*relative(path))


def twin(orig):
    """Human-readable location label for a duplicate's original (relative to iCloud)."""
    return where(os.path.normpath(os.path.join(ICLOUD, orig)))


def materialize(path, wait=120):
    """Ensure a cloud-only file is downloaded on macOS; always True on Linux mounts."""
    if not MAC:
        return True
    for attempt in range(wait // 3):
        try:
            with open(path, "rb") as handle:
                handle.read(1)
            return True
        except OSError:
            if attempt == 0:
                subprocess.run(["brctl", "download", path], capture_output=True)
            time.sleep(3)
    return False


def sig(path):
    """Signature of a file left in place (name|size on server; name|size|mtime on Mac)."""
    st = os.stat(path)
    if ROLE == "decide":
        return f"{nfc(os.path.basename(path))}|{st.st_size}"
    return f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}"


def load_kept():
    try:
        with open(KEPT, encoding="utf-8") as handle:
            return set(json.load(handle))
    except Exception:
        return set()


def save_kept(path, signature=None):
    kept = load_kept()
    kept.add(signature or sig(path))
    with open(KEPT, "w", encoding="utf-8") as handle:
        json.dump(sorted(kept), handle, ensure_ascii=False, indent=0)


def pending(sources, skip=lambda p: False):
    """Collect up to MAX_FILES ready files across sources in priority order."""
    now = time.time()
    kept = load_kept()
    files = []
    for src in sources:
        try:
            entries = sorted(os.listdir(src)) if os.path.isdir(src) else []
        except OSError as exc:
            log(f"ATTENTE source unreadable for now: {os.path.basename(src)} ({exc.strerror})")
            UNREADABLE.append(where(src))
            continue
        for name in entries:
            path = os.path.join(src, name)
            if name.startswith(".") or name.lower().endswith(PARTIAL_EXT) or not os.path.isfile(path):
                continue
            st = os.stat(path)
            if now - max(st.st_mtime, st.st_ctime) < MIN_AGE or sig(path) in kept or skip(path):
                continue
            if src == DOWNLOADS and (st.st_size > MAX_SIZE or not name.lower().endswith(DOCUMENTS)):
                if path not in TOO_BIG and st.st_size > MAX_SIZE and name.lower().endswith(DOCUMENTS):
                    TOO_BIG.add(path)
                    log(f"TROP GROS {name}  ({st.st_size // 2**20} MB) left in Downloads")
                    notify(f"{name} ({st.st_size // 2**20} Mo) trop gros, laissé dans Téléchargements")
                continue
            if (src == DRIVE and not name.lower().endswith(ROOT_DOCUMENTS)) or (
                src in CHROME_INBOXES and not name.lower().endswith(DOCUMENTS)
            ):
                continue
            files.append(path)
    return files[:MAX_FILES]


def extract(path):
    """Extract up to 2,500 chars of clean text from a document, or None if visual reading is needed."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            text = subprocess.run(
                ["pdftotext", "-l", "3", "-enc", "UTF-8", path, "-"],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        elif ext in ZIP_OFFICE_EXT:
            with zipfile.ZipFile(path) as zf:
                parts = [
                    name
                    for name in zf.namelist()
                    if re.match(r"(ppt/slides/slide\d+|xl/sharedStrings|word/document|content)\.xml$", name)
                ]
                text = " ".join(
                    re.sub(r"<[^>]+>", " ", zf.read(name).decode("utf-8", "replace")) for name in sorted(parts)[:30]
                )
        elif ext in TEXTUTIL_EXT and MAC:
            text = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", path],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        elif ext in TEXT_EXT:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read(6000)
        else:
            return None
    except Exception:
        return None
    text = re.sub(r"\s+", " ", text.replace("\x00", "")).strip()
    return text[:2500] if len(text) > 40 else None


def jpeg_preview(path, jpg):
    """Render a JPEG preview for HEIC/TIFF images (sips on macOS, heif-convert on Linux)."""
    if MAC:
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", path, "--out", jpg], capture_output=True)
    else:
        subprocess.run(["heif-convert", "-q", "85", path, jpg], capture_output=True, timeout=120)


def safe_dest(rel, tree):
    rel = nfc(os.path.normpath(rel.strip().strip("/")))
    if rel.startswith("..") or os.path.isabs(rel) or rel.startswith("00 ") or rel == ".":
        return None
    if rel in tree:
        return rel
    parent = os.path.dirname(rel)
    return rel if parent in tree else None


def clean_name(new, old):
    ext = os.path.splitext(old)[1]
    new = re.sub(r'[/:\\\n\r\t"*?<>|]', " ", new).strip()
    new = re.sub(r"\s+", " ", new)
    if not new:
        return old
    if not new.lower().endswith(ext.lower()):
        new = os.path.splitext(new)[0] + ext
    return new[:120]


def unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    idx = 2
    while os.path.exists(f"{base} ({idx}){ext}"):
        idx += 1
    return f"{base} ({idx}){ext}"


def sha(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def to_trash(path):
    subprocess.run(["trash", path], capture_output=True)
    if os.path.exists(path):
        shutil.move(path, unique(os.path.join(HOME, ".Trash", os.path.basename(path))))


def dedupe(files):
    """Split inbox files into (unique files to keep, [(duplicate, iCloud-relative original)])."""
    sizes = {os.path.getsize(p) for p in files}
    known = {}
    for root, dirs, names in os.walk(ICLOUD):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith("00 ")]
        for name in names:
            candidate = os.path.join(root, name)
            if not name.startswith(".") and os.path.getsize(candidate) in sizes and materialize(candidate, 30):
                try:
                    known.setdefault(sha(candidate), candidate)
                except OSError:
                    pass
    keep, duplicates = [], []
    for path in sorted(files, key=lambda x: (bool(re.search(r"\(\d+\)", x)), len(x))):
        digest = sha(path)
        if digest in known:
            duplicates.append((path, os.path.relpath(known[digest], ICLOUD)))
            if ROLE == "full" and not DRY:
                to_trash(path)
            if ROLE != "decide":
                log(
                    f"DOUBLON {os.path.basename(path)}  (identical to {os.path.relpath(known[digest], ICLOUD)}) -> Trash"
                )
        else:
            known[digest] = path
            keep.append(path)
    return keep, duplicates


def make_action(kind, src, **extra):
    """Build a plan.json action dict (preserving the French contract keys)."""
    root, rel_path = relative(src)
    size = os.path.getsize(src)
    ident = hashlib.sha1(f"{kind}|{root}|{rel_path}|{size}".encode()).hexdigest()[:16]
    return {
        "id": ident,
        "type": kind,
        "source": {"racine": root, "chemin": rel_path},
        "taille": size,
        "cree_le": time.time(),
        **extra,
    }
