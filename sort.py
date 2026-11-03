#!/usr/bin/env python3
"""Sorts the inboxes into the numbered folder tree, automatically: the Drive's 00 À trier (where the iPhone and the Mac's
Downloads put documents), the Drive's 00 Factures entrantes (invoices from Gmail), iCloud's 00 À trier, and last the Drive's
00 Pièces jointes (the attachments of the mails, exported by the mail sorter; a doubtful one stays there).
Claude (CLI, Read tool only) proposes a destination and a name for each file; this script validates them and files the
file: editable documents in the Drive, finished ones in iCloud. Nothing is ever overwritten; when in doubt, the file stays.

Ways to run it:
  sort.py [--dry-run]  everything on the Mac: decide and move (the original mode)
  sort.py --decide     on a server where iCloud and the Drive are mounted read-only: decides and writes plan.json (ranger,
                       doublon) for the Mac; with FILE_SORTER_DRIVE_RCLONE (an rclone remote that may write the Drive), it also
                       files the Drive's own documents itself: moves and renames inside the Drive, duplicates to the Drive's trash
  sort.py --apply      on the Mac: applies plan.json received from the server, pours the documents of ~/Downloads into the
                       Drive's 00 À trier, writes results.json
  sort.py --deposit <icloud|drive> <path> <file>   on the server: hands the Mac a document to write there (the VAT check report, for example)
Paths: FILE_SORTER_ICLOUD, FILE_SORTER_DRIVE, FILE_SORTER_DATA (plan, results, files left in place), FILE_SORTER_CLAUDE; the defaults are the Mac's.
FILE_SORTER_INBOX, FILE_SORTER_DRIVE_INBOX and FILE_SORTER_ATTACHMENTS_INBOX replace the inboxes for a trial, FILE_SORTER_MODEL the model (haiku),
FILE_SORTER_DOWNLOADS_MAX_MB the largest download poured into the Drive (50).
"""
import fcntl, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, unicodedata
from datetime import datetime
HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "settings.json"), encoding="utf-8") as f:
    SETTINGS = json.load(f)   # identity and personal paths, kept out of the repo (see settings.example.json)
MAC = sys.platform == "darwin"
if MAC:   # a launchd job inherits the system's default policy, which refuses to download a file that is only in the cloud
    # (EDEADLK, "Resource deadlock avoided"): this process allows itself to, for iCloud and Google Drive alike (setiopolicy_np(3))
    try:
        import ctypes
        ctypes.CDLL("/usr/lib/libSystem.B.dylib").setiopolicy_np(3, 0, 2)   # VFS_MATERIALIZE_DATALESS_FILES, SCOPE_PROCESS, ON
    except (OSError, AttributeError):
        pass
ROLE = "decide" if "--decide" in sys.argv else "apply" if "--apply" in sys.argv else "deposit" if "--deposit" in sys.argv else "full"
ICLOUD = os.environ.get("FILE_SORTER_ICLOUD") or os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
DRIVE = os.environ.get("FILE_SORTER_DRIVE") or os.path.join(HOME, SETTINGS["drive"])
DATA = os.environ.get("FILE_SORTER_DATA") or HERE
INBOX = os.environ.get("FILE_SORTER_INBOX") or os.path.join(ICLOUD, "00 À trier")   # FILE_SORTER_INBOX: another inbox, for a trial
DRIVE_INBOX = os.environ.get("FILE_SORTER_DRIVE_INBOX") or os.path.join(DRIVE, "00 À trier")   # the single inbox: iPhone, Mac's Downloads
DOWNLOADS = os.path.join(HOME, "Downloads")
GMAIL_INBOX = os.path.join(DRIVE, "00 Factures entrantes")  # filled by gmail-invoices.gs
# the attachments of the mails, received and sent, exported by the mail sorter (attachments.py): read last, so that Benjamin's own
# inboxes go first; a doubtful file stays there, remembered in kept.json, rather than flooding 00 À trier with a mailbox's backlog
ATTACH_INBOX = os.environ.get("FILE_SORTER_ATTACHMENTS_INBOX") or os.path.join(DRIVE, "00 Pièces jointes")
DRIVE_INBOXES = (DRIVE_INBOX, GMAIL_INBOX, ATTACH_INBOX)
DRIVE_RCLONE = os.environ.get("FILE_SORTER_DRIVE_RCLONE")   # server: rclone remote allowed to write the Drive ("gsort:"); unset, only the Mac moves
EXTERNAL = (DOWNLOADS, GMAIL_INBOX)   # sources Benjamin does not look at: a doubtful file goes to the Drive's 00 À trier
OFFICE = (".docx", ".doc", ".odt", ".xlsx", ".xls", ".ods", ".pptx", ".ppt", ".odp")   # editable: filed in the Drive (same tree), finished documents in iCloud
DOCUMENTS = OFFICE + (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff", ".rtf",
                      ".pages", ".numbers", ".key", ".txt", ".csv", ".md")   # what Downloads pours into the Drive; the rest stays there
PREVIEWS = tempfile.mkdtemp(prefix="file-sorter-")
PARTIAL_EXT = (".crdownload", ".download", ".part", ".partial", ".tmp", ".opdownload")
MAX_SIZE = int(os.environ.get("FILE_SORTER_DOWNLOADS_MAX_MB", "50")) * 1024 * 1024   # above this, a download stays in Downloads
LOG = os.path.join(HOME, "Library/Logs/file-sorter.log") if MAC else None   # elsewhere: standard output, kept by the service's journal
LOCK = os.path.join(DATA, "sort.lock")
KEPT = os.path.join(DATA, "kept.json")   # files left in place, not to be asked about again
PLAN = os.path.join(DATA, "plan.json")
RESULTS = os.path.join(DATA, "results.json")
APPLIED = os.path.join(DATA, "applied.json")   # Mac: actions already applied, kept 7 days
CLAUDE = os.environ.get("FILE_SORTER_CLAUDE") or "/opt/homebrew/bin/claude"
MODEL = os.environ.get("FILE_SORTER_MODEL", "haiku")
MIN_AGE = 60          # seconds without modification before a file is handled
MAX_FILES = 15        # per pass
THRESHOLD = 0.7       # minimum confidence to move a file
DRY = "--dry-run" in sys.argv
TEXT_EXT = {".txt", ".csv", ".ics", ".md", ".json", ".html", ".htm", ".xml"}
TEXTUTIL_EXT = {".doc", ".docx", ".rtf", ".odt", ".pages"}
VISION_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
nfc = lambda s: unicodedata.normalize("NFC", s)

# Log lines start with a French keyword (RANGÉ, GARDÉ, DOUBLON, DÉPOSÉ, VERSÉ, LAISSÉ, PLAN, MAC, ERREUR, SANS RÉPONSE, ATTENTE, TROP GROS,
# À DÉPOSER): docs and habits grep the journals for them, so they stay as they are. Notifications stay in French too: the owner reads them.


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    if LOG:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    print(line, flush=True)


def notify(msg):
    if not MAC:   # server: `notifier` goes to ntfy, which is public: it only gets messages without file names
        subprocess.run(["notifier", "Tri des fichiers", msg], check=False, capture_output=True)
        return
    subprocess.run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "Tri iCloud"',
                    "-e", "end run", msg], check=False)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def folders():
    out = []
    for r, ds, _ in os.walk(ICLOUD):
        ds[:] = sorted(d for d in ds if not d.startswith("."))
        rel = nfc(os.path.relpath(r, ICLOUD))
        if rel == "." or rel.startswith("00 "):
            continue
        if rel.count(os.sep) < 4:
            out.append(rel)
    return out


def materialize(p, wait=120):
    """Downloads a file that is only in the cloud (reading it does, with the policy set above; brctl download as a fallback
    for iCloud). False if it stays unavailable. Off the Mac, the mount takes care of it."""
    if not MAC:
        return True
    for i in range(wait // 3):
        try:
            with open(p, "rb") as fh:
                fh.read(1)
            return True
        except OSError:
            if i == 0:
                subprocess.run(["brctl", "download", p], capture_output=True)
            time.sleep(3)
    return False


def sig(p):
    """Signature of a file left in place. The server keeps only name and size: a file the Mac moves keeps both, not its date."""
    st = os.stat(p)
    return f"{nfc(os.path.basename(p))}|{st.st_size}" if ROLE == "decide" else f"{os.path.basename(p)}|{st.st_size}|{int(st.st_mtime)}"


def load_kept():
    try:
        return set(json.load(open(KEPT)))
    except Exception:
        return set()


def save_kept(p, signature=None):   # signature: taken before the server moved the file away
    kept = load_kept()
    kept.add(signature or sig(p))
    json.dump(sorted(kept), open(KEPT, "w"), ensure_ascii=False, indent=0)


TOO_BIG = set()


def pending(sources, skip=lambda p: False):
    """The files to decide, MAX_FILES at most, in the order of the sources. skip: files already planned or handled, left out before
    the count, so that files waiting for the Mac never hold back the next ones."""
    now = time.time()
    kept = load_kept()
    files = []
    for src in sources:
        try:
            entries = sorted(os.listdir(src)) if os.path.isdir(src) else []
        except OSError as e:   # Drive/iCloud folder being synced (EDEADLK): retried on the next pass, without an alert
            log(f"ATTENTE source unreadable for now: {os.path.basename(src)} ({e.strerror})")
            continue
        for f in entries:
            p = os.path.join(src, f)
            if f.startswith(".") or f.lower().endswith(PARTIAL_EXT) or not os.path.isfile(p):
                continue
            st = os.stat(p)
            if now - max(st.st_mtime, st.st_ctime) < MIN_AGE or sig(p) in kept or skip(p):
                continue
            if src == DOWNLOADS and (st.st_size > MAX_SIZE or not f.lower().endswith(DOCUMENTS)):
                if p not in TOO_BIG and st.st_size > MAX_SIZE and f.lower().endswith(DOCUMENTS):
                    TOO_BIG.add(p)
                    log(f"TROP GROS {f}  ({st.st_size // 2**20} MB) left in Downloads")
                    notify(f"{f} ({st.st_size // 2**20} Mo) trop gros, laissé dans Téléchargements")
                continue
            files.append(p)
    return files[:MAX_FILES]


def extract(p):
    """Short text of the file, or None when it has to be read visually."""
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == ".pdf":
            t = subprocess.run(["pdftotext", "-l", "3", "-enc", "UTF-8", p, "-"],
                               capture_output=True, text=True, timeout=60).stdout
        elif ext in TEXTUTIL_EXT and MAC:
            t = subprocess.run(["textutil", "-convert", "txt", "-stdout", p],
                               capture_output=True, text=True, timeout=60).stdout
        elif ext in (".pptx", ".xlsx", ".docm", ".xlsm") or (ext == ".docx" and not MAC):
            import zipfile
            with zipfile.ZipFile(p) as z:
                parts = [n for n in z.namelist() if re.match(r"(ppt/slides/slide\d+|xl/sharedStrings|word/document)\.xml$", n)]
                t = " ".join(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "replace")) for n in sorted(parts)[:30])
        elif ext in TEXT_EXT:
            t = open(p, errors="replace").read(6000)
        else:
            return None
    except Exception:
        return None
    t = re.sub(r"\s+", " ", t).strip()
    return t[:2500] if len(t) > 40 else None


# Answer format imposed on Claude. The keys are French and part of the exchange with the model: they stay as they are.
SCHEMA = {
    "type": "object",
    "properties": {"fichiers": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "fichier": {"type": "string"},
            "destination": {"type": "string"},
            "nouveau_nom": {"type": ["string", "null"]},
            "confiance": {"type": "number"},
            "raison": {"type": "string"},
        },
        "required": ["fichier", "destination", "nouveau_nom", "confiance", "raison"],
    }}},
    "required": ["fichiers"],
}


CONVENTION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "convention.md")).read()
RULES = open(os.path.join(HERE, "rules.md"), encoding="utf-8").read().strip()


def jpeg_preview(p, jpg):
    """JPEG preview of an image the Read tool cannot read (HEIC, TIFF): sips on the Mac, heif-convert elsewhere."""
    if MAC:
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", p, "--out", jpg], capture_output=True)
    else:
        subprocess.run(["heif-convert", "-q", "85", p, jpg], capture_output=True, timeout=120)


def ask_claude(items, tree):
    # The prompt stays in French on purpose: it carries the owner's French rules and naming convention, and the names
    # it produces are French. Rewording it would change the names and destinations Claude chooses.
    blocks = []
    for p, txt in items:
        name = os.path.basename(p)
        if txt:
            blocks.append(f"### {name}\nExtrait du texte :\n{txt}")
        elif os.path.splitext(p)[1].lower() in (".heic", ".heif", ".tiff"):
            jpg = os.path.join(PREVIEWS, re.sub(r"\W", "_", name) + ".jpg")
            jpeg_preview(p, jpg)
            blocks.append(f"### {name}\nImage : lis l'aperçu JPEG avec l'outil Read à ce chemin exact : {jpg}")
        else:
            blocks.append(f"### {name}\nPas de texte extractible : lis-le avec l'outil Read à ce chemin exact : {p}")
    prompt = f"""Tu ranges les documents personnels de {SETTINGS['proprietaire']} dans son iCloud Drive.

Dossiers existants (chemins relatifs, à réutiliser tels quels) :
{chr(10).join('- ' + f for f in tree)}

Règles :
- "destination" = un dossier de la liste ci-dessus. Tu peux ajouter UN niveau de sous-dossier sous un dossier existant si c'est clairement utile (ex. "50 Travail/Auto-entrepreneur/Factures 2026/T4" pour une facture d'octobre 2026 ; trimestres T1=jan-mars, T2=avr-juin, T3=juil-sept, T4=oct-déc).
{RULES}
- "nouveau_nom" : TOUJOURS fourni, conforme à la convention ci-dessous (null uniquement si le nom actuel la respecte déjà exactement).

{CONVENTION}
- "confiance" entre 0 et 1. Si tu n'es pas sûr, mets moins de 0.7 : le fichier restera dans "00 À trier".
- "raison" : une phrase courte.
- Le champ "fichier" doit reprendre EXACTEMENT le nom donné après "###". Réponds pour CHAQUE fichier.

Fichiers à classer :

{chr(10).join(blocks)}
"""
    dirs = [x for d in (INBOX, DRIVE_INBOX, DOWNLOADS, GMAIL_INBOX, ATTACH_INBOX, PREVIEWS) if os.path.isdir(d) for x in ("--add-dir", d)]
    cmd = [CLAUDE, "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--tools", "Read", "--allowedTools", "Read",
           *dirs, "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir(), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude failed ({r.returncode}): {r.stderr[-500:] or r.stdout[-500:]}")
    data = json.loads(r.stdout)
    res = data.get("structured_output") or data.get("result")
    if isinstance(res, str):
        res = json.loads(re.search(r"\{.*\}", res, re.S).group(0))
    return res["fichiers"]


def safe_dest(rel, tree):
    rel = nfc(os.path.normpath(rel.strip().strip("/")))
    if rel.startswith("..") or os.path.isabs(rel) or rel.startswith("00 ") or rel == ".":
        return None
    if rel in tree:
        return rel
    parent = os.path.dirname(rel)
    return rel if parent in tree else None   # one new level at most


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
    b, e = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{b} ({i}){e}"):
        i += 1
    return f"{b} ({i}){e}"


def sha(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def to_trash(p):
    subprocess.run(["trash", p], capture_output=True)
    if os.path.exists(p):   # trash sometimes refuses iCloud files
        shutil.move(p, unique(os.path.join(HOME, ".Trash", os.path.basename(p))))


def dedupe(files):
    """Finds the files already present, identical, in iCloud (or earlier in the queue): [files to keep…], [(duplicate, its original)].
    In full mode the duplicate goes to the Trash at once; in decide mode, the Mac will send it there."""
    sizes = {os.path.getsize(p) for p in files}
    known = {}
    for r, ds, fs in os.walk(ICLOUD):
        ds[:] = [d for d in ds if not d.startswith(".") and not d.startswith("00 ")]
        for f in fs:
            q = os.path.join(r, f)
            if not f.startswith(".") and os.path.getsize(q) in sizes and materialize(q, 30):
                try:
                    known.setdefault(sha(q), q)
                except OSError:
                    pass
    keep, duplicates = [], []
    for p in sorted(files, key=lambda x: (bool(re.search(r"\(\d+\)", x)), len(x))):
        h = sha(p)
        if h in known:
            duplicates.append((p, os.path.relpath(known[h], ICLOUD)))
            if ROLE == "full" and not DRY:
                to_trash(p)
            if ROLE != "decide":
                log(f"DOUBLON {os.path.basename(p)}  (identical to {os.path.relpath(known[h], ICLOUD)}) -> Trash")
        else:
            known[h] = p
            keep.append(p)
    return keep, duplicates


def choose(files):
    """Claude's decision for each file, validated: [(source, destination or None if the file stays, name, confidence, answer)]."""
    tree = folders()
    items = [(p, extract(p)) for p in files]
    decisions = ask_claude(items, tree)
    simple = lambda x: re.sub(r"[^0-9a-z]", "", unicodedata.normalize("NFKD", x).lower())
    by_name = {simple(os.path.basename(p)): p for p in files}
    answered, choices = set(), []
    for d in decisions:
        src = by_name.get(simple(d.get("fichier", "")))
        if not src or src in answered or not os.path.exists(src):
            continue
        answered.add(src)
        name = os.path.basename(src)
        dest = safe_dest(d.get("destination", ""), tree)
        conf = float(d.get("confiance", 0))
        if not dest or conf < THRESHOLD:
            choices.append((src, None, name, conf, d))
            continue
        new = name
        keeps_name = dest.startswith("80 Images/Photos") or dest.startswith("80 Images/Captures")   # photos and screenshots keep theirs
        if d.get("nouveau_nom") and not keeps_name:
            new = clean_name(d["nouveau_nom"], name)
        if new == name and not keeps_name and os.path.dirname(src) == ATTACH_INBOX:
            # an exported attachment's name never follows the convention: no name proposed means the model meant something else
            # (a copy of another document of the batch, to throw away, which the answer cannot say): the file stays in its inbox
            choices.append((src, None, name, conf, d))
            continue
        choices.append((src, dest, new, conf, d))
    for p in files:
        if p not in answered:
            log(f"SANS RÉPONSE {os.path.basename(p)}  (retried on the next pass)")
    return choices


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # released automatically if the process dies
    except OSError:
        return
    try:
        moved, kept = [], []
        done = set()
        while True:
            files = pending((DOWNLOADS, GMAIL_INBOX, DRIVE_INBOX, INBOX, ATTACH_INBOX), skip=lambda p: p in done)
            if not files:
                break
            done.update(files)
            ready = []
            for p in files:
                if materialize(p):
                    ready.append(p)
                else:
                    log(f"ATTENTE {os.path.basename(p)}  (not downloaded from iCloud yet, retried on the next pass)")
            files = dedupe(ready)[0]
            if files:
                batch(files, moved, kept)
        if moved or kept:
            msg = f"{len(moved)} rangé(s)" + (f", {len(kept)} laissé(s) dans À trier" if kept else "")
            if len(moved) == 1:
                msg = moved[0]
            notify(msg)
    except Exception as e:
        log(f"ERREUR  {e}")
        notify(f"Erreur du tri : {e}"[:200])
    finally:
        lock.close()
    audit()


def audit():
    """Tidiness state for the Dashboard (no AI, names and dates only)."""
    subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.py")], capture_output=True, timeout=300)


def batch(files, moved, kept):
    for src, dest, new, conf, d in choose(files):
        name = os.path.basename(src)
        if dest is None:
            if os.path.dirname(src) != ATTACH_INBOX:   # a doubtful attachment stays in its inbox, without a notification
                kept.append(name)
            if not DRY and os.path.dirname(src) in EXTERNAL:
                os.makedirs(DRIVE_INBOX, exist_ok=True)
                target = unique(os.path.join(DRIVE_INBOX, name))
                shutil.move(src, target)
                save_kept(target)
            elif not DRY:
                save_kept(src)
            log(f"GARDÉ   {name}  (confidence {conf:.2f}, proposed: {d.get('destination')}) — {d.get('raison')}")
            continue
        base = DRIVE if name.lower().endswith(OFFICE) else ICLOUD
        target = unique(os.path.join(base, dest, new))
        if DRY:
            log(f"[dry run] {name}  ->  {os.path.relpath(target, ICLOUD)}  ({conf:.2f}) — {d.get('raison')}")
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(src, target)
        moved.append(f"{new} → {dest}")
        log(f"RANGÉ   {name}  ->  {'Drive/' if base == DRIVE else ''}{os.path.relpath(target, base)}  ({conf:.2f}) — {d.get('raison')}")


# ---- decision on the server, move on the Mac ----
# plan.json and results.json are exchanged between the server and the Mac: their French keys
# (actions, faites, appliques, racine, chemin, taille, cible, cree_le...), action types and statuses are a contract.

def relative(p):
    """(root, path relative to it in NFC) of a file in iCloud or the Drive."""
    return ("drive", nfc(os.path.relpath(p, DRIVE))) if p.startswith(DRIVE + os.sep) else ("icloud", nfc(os.path.relpath(p, ICLOUD)))


def make_action(kind, src, **extra):
    # the keyword arguments (original, cible, confiance, raison) become keys of the plan
    root, path = relative(src)
    size = os.path.getsize(src)
    ident = hashlib.sha1(f"{kind}|{root}|{path}|{size}".encode()).hexdigest()[:16]
    return {"id": ident, "type": kind, "source": {"racine": root, "chemin": path}, "taille": size, "cree_le": time.time(), **extra}


class DriveError(RuntimeError):
    """A move in the Drive failed: the pass ends in error once everything else is done, and the server's monitor reports it."""


def rclone(*args):
    try:
        r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise DriveError(f"rclone {args[0]}: no answer from Google within 5 minutes")
    if r.returncode:
        raise DriveError(f"rclone {args[0]}: {r.stderr.strip()[-300:]}")
    return r.stdout


def drive_unique(folder, name):
    """A name not taken in a Drive folder, read from Google (the mount shows changes a few minutes late): the Drive accepts
    two files with the same name, a move would never fail on one, so the check comes first."""
    try:
        taken = {nfc(n.rstrip("/")) for n in rclone("lsf", "--max-depth", "1", DRIVE_RCLONE + folder).splitlines()}
    except DriveError as e:
        if "directory not found" not in str(e):
            raise
        taken = set()   # a new folder: the move creates it
    b, e = os.path.splitext(name)
    i, candidate = 2, name
    while nfc(candidate) in taken:
        candidate, i = f"{b} ({i}){e}", i + 1
    return candidate


def drive_move(src, folder, name):
    """Server: moves and renames a file of a Drive inbox inside the Drive, in one call to Google. Returns the new path."""
    target = f"{folder}/{drive_unique(folder, name)}"
    rclone("moveto", DRIVE_RCLONE + os.path.relpath(src, DRIVE), DRIVE_RCLONE + target)   # the name as the mount shows it: Google's
    return target


def decide():
    """Server: decides for the inboxes (the Drive's 00 À trier and 00 Factures entrantes, iCloud's 00 À trier, the Drive's 00 Pièces
    jointes). Into iCloud, and out of iCloud, only the Mac moves: plan.json keeps the actions it has not returned yet. Inside the Drive,
    with FILE_SORTER_DRIVE_RCLONE, the server files itself: editable documents into the tree, doubtful invoices into 00 À trier,
    duplicates to the Drive's trash. A source already planned or just handled is not asked about again."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    failures = []
    try:
        plan = read_json(PLAN, {"actions": [], "faites": {}})
        returned = read_json(RESULTS, {}).get("appliques", {})
        done = {k: v for k, v in plan.get("faites", {}).items() if time.time() - v[1] < 3600}   # sources handled, until the mounts show it
        actions = []
        for a in plan.get("actions", []):
            if a["id"] in returned:
                done[f"{a['source']['racine']}|{a['source']['chemin']}"] = [returned[a["id"]], time.time()]
                log(f"MAC     {a['type']} {a['source']['chemin']} : {returned[a['id']]}")
            elif time.time() - a.get("cree_le", 0) < 7 * 86400:   # an action not returned within a week is dropped
                actions.append(a)
        planned = {f"{a['source']['racine']}|{a['source']['chemin']}" for a in actions} | set(done)
        files = pending((GMAIL_INBOX, DRIVE_INBOX, INBOX, ATTACH_INBOX), skip=lambda p: "|".join(relative(p)) in planned)
        server_side = lambda p: bool(DRIVE_RCLONE) and os.path.dirname(p) in DRIVE_INBOXES

        def by_server(p, what, act):   # a move in the Drive done here; a failure is kept for the end of the pass, the file stays
            try:
                result = act()
                done["|".join(relative(p))] = [f"ok (serveur) : {result}", time.time()]
                return result
            except DriveError as e:
                failures.append(os.path.basename(p))
                log(f"ERREUR  {what} {os.path.basename(p)} : {e}")
                return None

        if files:
            files, duplicates = dedupe(files)
            for p, orig in duplicates:
                if server_side(p):
                    if by_server(p, "doublon", lambda: rclone("deletefile", "--drive-use-trash", DRIVE_RCLONE + os.path.relpath(p, DRIVE)) or "corbeille du Drive"):
                        log(f"DOUBLON {os.path.basename(p)}  (identical to {orig}) -> the Drive's trash, by the server")
                else:
                    actions.append(make_action("doublon", p, original=nfc(orig)))
                    log(f"DOUBLON {os.path.basename(p)}  (identical to {orig}) -> Trash, by the Mac")
            if files:
                for src, dest, new, conf, d in choose(files):
                    name = os.path.basename(src)
                    if dest is None:
                        signature = sig(src)   # before a move: the mount may stop showing the file at once
                        if os.path.dirname(src) == GMAIL_INBOX:   # a doubtful invoice joins the inbox Benjamin looks at; an attachment stays
                            if server_side(src):
                                if not by_server(src, "vers 00 À trier", lambda: drive_move(src, os.path.relpath(DRIVE_INBOX, DRIVE), name)):
                                    continue
                            else:
                                actions.append(make_action("vers_a_trier", src))
                        save_kept(src, signature)
                        log(f"GARDÉ   {name}  (confidence {conf:.2f}, proposed: {d.get('destination')}) — {d.get('raison')}")
                        continue
                    base = "drive" if name.lower().endswith(OFFICE) else "icloud"
                    if base == "drive" and server_side(src):
                        target = by_server(src, "rangement", lambda: drive_move(src, dest, new))
                        if target:
                            log(f"RANGÉ   {name}  ->  Drive/{target}  ({conf:.2f}) — {d.get('raison')} (by the server)")
                        continue
                    actions.append(make_action("ranger", src, cible={"racine": base, "chemin": nfc(os.path.join(dest, new))}, confiance=conf, raison=d.get("raison", "")))
                    log(f"PLAN    {name}  ->  {'Drive/' if base == 'drive' else ''}{os.path.join(dest, new)}  ({conf:.2f}) — {d.get('raison')}")
        write_json(PLAN, {"genere_a": datetime.now().isoformat(timespec="seconds"), "actions": actions, "faites": done})
    except Exception as e:
        log(f"ERREUR  {e}")
        notify("Erreur du tri des fichiers sur le serveur : voir son journal")
        raise
    finally:
        lock.close()
    if failures:   # after the plan is written; no notification every 5 minutes: the monitor reports a failed pass once
        log(f"ERREUR  {len(failures)} move(s) in the Drive failed, retried on the next pass: {', '.join(failures)}")
        sys.exit(1)


def apply_plan():
    """Mac: applies plan.json received from the server, pours ~/Downloads into 00 À trier, writes results.json (what was done, or why not)."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    try:
        plan = read_json(PLAN, {"actions": []})
        applied = {k: v for k, v in read_json(APPLIED, {}).items() if time.time() - v[1] < 7 * 86400}
        moved, kept = [], []
        for a in plan.get("actions", []):
            if a["id"] in applied:
                continue
            try:
                status = apply_action(a, moved, kept)
            except Exception as e:
                status = f"erreur : {type(e).__name__} {e}"[:200]
                log(f"ERREUR  {a['type']} {a['source']['chemin']} : {status}")
            if status:
                applied[a["id"]] = [status, time.time()]
        move_downloads()
        write_json(APPLIED, applied)
        write_json(RESULTS, {"rendu_le": datetime.now().isoformat(timespec="seconds"), "appliques": {k: v[0] for k, v in applied.items()}})
        if moved or kept:
            msg = f"{len(moved)} rangé(s)" + (f", {len(kept)} laissé(s) dans À trier" if kept else "")
            notify(moved[0] if len(moved) == 1 and not kept else msg)
    except Exception as e:
        log(f"ERREUR  {e}")
        notify(f"Erreur du tri : {e}"[:200])
    finally:
        lock.close()
    audit()


def apply_action(a, moved, kept):
    """One action of the plan, after checks: paths inside iCloud or the Drive, source in an inbox, size unchanged.
    Returns the status to send back to the server, or None to retry on the next pass (file not downloaded yet).
    The statuses and the error messages below stay in French: they go back to the server in results.json."""
    roots = {"icloud": ICLOUD, "drive": DRIVE}
    def resolve(c):
        base = roots[c["racine"]]
        p = os.path.normpath(os.path.join(base, nfc(c["chemin"])))
        if not p.startswith(base + os.sep) or ".." in c["chemin"].split("/"):
            raise ValueError("chemin hors d'iCloud et du Drive")
        return p
    if a["type"] == "deposer":   # document handed over by the server, written here without ever overwriting another one
        if a["cible"]["chemin"].startswith("00 "):
            raise ValueError("destination dans une boîte d'arrivée")
        target = unique(resolve(a["cible"]))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(a["contenu"])
        moved.append(f"{os.path.basename(target)} → {os.path.dirname(a['cible']['chemin'])}")
        log(f"DÉPOSÉ  {os.path.relpath(target, roots[a['cible']['racine']])}")
        return "ok"
    src = resolve(a["source"])
    if nfc(os.path.dirname(src)) not in (nfc(INBOX), nfc(GMAIL_INBOX), nfc(DRIVE_INBOX), nfc(ATTACH_INBOX)):
        raise ValueError("source hors des boîtes d'arrivée")
    if not os.path.exists(src):
        return "absent"
    if not materialize(src):
        return None
    if os.path.getsize(src) != a["taille"]:
        return "modifié depuis la décision"
    name = os.path.basename(src)
    if a["type"] == "doublon":
        to_trash(src)
        log(f"DOUBLON {name}  (identical to {a.get('original')}) -> Trash")
    elif a["type"] == "vers_a_trier":   # from a server without Drive access: into the Drive's 00 À trier
        shutil.move(src, unique(os.path.join(DRIVE_INBOX, name)))
        kept.append(name)
        log(f"GARDÉ   {name}  -> 00 À trier (server's decision)")
    elif a["type"] == "ranger":
        if a["cible"]["chemin"].startswith("00 "):
            raise ValueError("destination dans une boîte d'arrivée")
        target = unique(resolve(a["cible"]))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(src, target)
        dest = os.path.dirname(a["cible"]["chemin"])
        moved.append(f"{os.path.basename(target)} → {dest}")
        log(f"RANGÉ   {name}  ->  {'Drive/' if a['cible']['racine'] == 'drive' else ''}{os.path.relpath(target, roots[a['cible']['racine']])}  ({a.get('confiance', 0):.2f}) — {a.get('raison', '')}")
    else:
        raise ValueError(f"action inconnue : {a['type']}")
    return "ok"


def deposit():
    """Server: adds to the plan a document for the Mac to write ("deposer" action), waiting if needed for a running decision to finish."""
    i = sys.argv.index("--deposit")
    root, rel, source_file = sys.argv[i + 1:i + 4]
    if root not in ("icloud", "drive") or rel.startswith("00 ") or ".." in rel.split("/"):
        sys.exit("destination refused")
    content = open(source_file, encoding="utf-8").read()
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan = read_json(PLAN, {"actions": [], "faites": {}})
        target = {"racine": root, "chemin": nfc(rel)}
        ident = hashlib.sha1(f"deposer|{root}|{rel}|{content}".encode()).hexdigest()[:16]
        if all(a["id"] != ident for a in plan["actions"]):
            plan["actions"].append({"id": ident, "type": "deposer", "source": target, "cible": target, "contenu": content, "cree_le": time.time()})
            write_json(PLAN, plan)
    log(f"À DÉPOSER {rel} ({len(content)} characters): the Mac will write it on its next pass")


def move_downloads():
    """Mac: the documents of ~/Downloads go into the Drive's 00 À trier, where the server sees them and decides. What is not a
    document (installers, archives, videos...) or is too big stays in Downloads, reported once; nothing is ever deleted there."""
    if not os.path.isdir(DOWNLOADS) or not os.path.isdir(DRIVE):   # Google Drive for desktop not running: they wait
        return
    kept = load_kept()
    for f in sorted(os.listdir(DOWNLOADS)):
        p = os.path.join(DOWNLOADS, f)
        if f.startswith(".") or f.lower().endswith(PARTIAL_EXT) or not os.path.isfile(p) or sig(p) in kept:
            continue
        st = os.stat(p)
        if time.time() - max(st.st_mtime, st.st_ctime) < MIN_AGE:
            continue
        if not f.lower().endswith(DOCUMENTS):
            save_kept(p)
            log(f"LAISSÉ  {f}  (not a document) left in Downloads")
            continue
        if st.st_size > MAX_SIZE:
            save_kept(p)
            log(f"TROP GROS {f}  ({st.st_size // 2**20} MB) left in Downloads")
            notify(f"{f} ({st.st_size // 2**20} Mo) trop gros pour le Drive, laissé dans Téléchargements")
            continue
        os.makedirs(DRIVE_INBOX, exist_ok=True)
        shutil.move(p, unique(os.path.join(DRIVE_INBOX, f)))
        log(f"VERSÉ   {f}  -> Drive/00 À trier")


if __name__ == "__main__":
    # an unknown or former option stops here: falling back to the full mode would decide and move files
    args = sys.argv[1:]
    if "--deposit" in args:
        args = args[:args.index("--deposit") + 1] + args[args.index("--deposit") + 4:]
    unknown = [a for a in args if a not in ("--dry-run", "--decide", "--apply", "--deposit")]
    if unknown:
        sys.exit(f"unknown argument {unknown[0]}: sort.py [--dry-run] | --decide | --apply | --deposit <icloud|drive> <path> <file>")
    {"decide": decide, "apply": apply_plan, "deposit": deposit}.get(ROLE, main)()
