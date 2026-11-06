#!/usr/bin/env python3
"""Sorts the inboxes into the numbered folder tree, automatically: the Drive's 00 À trier (where the iPhone and the Mac's
Downloads put documents), the Drive's 00 Factures entrantes (invoices from Gmail), the documents the iPhone saves into the Drive
without a folder (Chrome's « Enregistré depuis Chrome », the Drive's root), iCloud's 00 À trier, and last the Drive's 00 Pièces
jointes (the attachments of the mails, exported by the mail sorter; a doubtful one stays there).
A model proposes a destination and a name for each file: the home server's local model for the files whose text can be read (an
OpenAI API, no tool, a strict JSON schema), Claude (CLI, Read tool only) for the others (scans, images: the local model reads no image).
This script validates them and files the file: editable documents in the Drive, finished ones in iCloud. Nothing is ever overwritten;
when in doubt, the file stays. With FILE_SORTER_SHADOW=1, Claude decides for every file and the local model only answers alongside
for the files with text: each disagreement on the folder or the name is a « shadow: » line of the journal.

Ways to run it:
  sort.py [--dry-run]  everything on the Mac: decide and move (the original mode)
  sort.py --decide     on a server where iCloud and the Drive are mounted read-only: decides and writes plan.json (ranger,
                       doublon) for the Mac; with FILE_SORTER_DRIVE_RCLONE (an rclone remote that may write the Drive), it also
                       files the Drive's own documents itself: moves and renames inside the Drive, duplicates to the Drive's trash.
                       When the inboxes leave the models free, it also reviews the theme folders, a few documents per pass: each one
                       is kept, archived (« 90 Archives », same path) or trashed (plan actions archiver and supprimer for iCloud);
                       FILE_SORTER_CLEANUP=trial (the default) only logs these decisions, on applies them, off stops the review
  sort.py --apply      on the Mac: applies plan.json received from the server, pours the documents of ~/Downloads into the
                       Drive's 00 À trier, writes results.json
  sort.py --deposit <icloud|drive> <path> <file>   on the server: hands the Mac a document to write there (the VAT check report, for example)
Paths: FILE_SORTER_ICLOUD, FILE_SORTER_DRIVE, FILE_SORTER_DATA (plan, results, files left in place), FILE_SORTER_CLAUDE; the defaults are the Mac's.
FILE_SORTER_LOCAL_URL: the local model (default 127.0.0.1:11234; "off": none, the files with text wait), FILE_SORTER_SHADOW=1: see above.
With --decide, every decision and action, the server's and the Mac's (read back from results.json), goes to activity.jsonl, and each step of
the pass to run-status.json, both in FILE_SORTER_DATA, for the home server's Dashboard (activity.py); the Mac writes neither.
FILE_SORTER_INBOX, FILE_SORTER_DRIVE_INBOX and FILE_SORTER_ATTACHMENTS_INBOX replace the inboxes for a trial, FILE_SORTER_MODEL the model (haiku),
FILE_SORTER_DOWNLOADS_MAX_MB the largest download poured into the Drive (50), FILE_SORTER_MIN_AGE the seconds a file must stay unchanged
before it is handled (60; tests.py sets 0).
"""
import atexit, fcntl, hashlib, json, os, re, shutil, signal, stat, subprocess, sys, tempfile, time, unicodedata, urllib.error, urllib.request
from datetime import datetime
HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)   # python3 -I leaves the script's folder out of the import path
import activity  # noqa: E402
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
# What the iPhone saves into the Drive without choosing a folder (05/11/2026, ticket 14): Chrome's « Enregistrer dans Drive » puts it in
# a folder of its own at the Drive's root (its name follows Chrome's language), the Files app at the root itself. Both are sorted like an
# inbox, documents only (the rest stays where it is); a doubtful one goes to the Drive's 00 À trier, so that the root stays tidy.
CHROME_NAMES = ("Enregistré depuis Chrome", "Saved from Chrome")


def chrome_inboxes():
    """Chrome's folders as the Drive shows them (their names compared in NFC: the mount may show « é » decomposed), else the usual names."""
    try:
        found = [os.path.join(DRIVE, d) for d in sorted(os.listdir(DRIVE))
                 if unicodedata.normalize("NFC", d) in CHROME_NAMES and os.path.isdir(os.path.join(DRIVE, d))]
    except OSError:
        found = []
    return tuple(found) or tuple(os.path.join(DRIVE, d) for d in CHROME_NAMES)


CHROME_INBOXES = chrome_inboxes()
DRIVE_INBOXES = (DRIVE_INBOX, GMAIL_INBOX, ATTACH_INBOX) + CHROME_INBOXES + (DRIVE,)   # DRIVE: the loose documents of its root
DRIVE_RCLONE = os.environ.get("FILE_SORTER_DRIVE_RCLONE")   # server: rclone remote allowed to write the Drive ("gsort:"); unset, only the Mac moves
EXTERNAL = (DOWNLOADS, GMAIL_INBOX) + CHROME_INBOXES + (DRIVE,)   # sources the owner does not look at: a doubtful file goes to the Drive's 00 À trier
OFFICE = (".docx", ".doc", ".odt", ".xlsx", ".xls", ".ods", ".pptx", ".ppt", ".odp")   # editable: filed in the Drive (same tree), finished documents in iCloud
DOCUMENTS = OFFICE + (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff", ".rtf",
                      ".pages", ".numbers", ".key", ".txt", ".csv", ".md")   # what Downloads pours into the Drive; the rest stays there
ROOT_DOCUMENTS = OFFICE + (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".rtf", ".pages", ".numbers", ".key")   # at the Drive's root: never a text or data file a program may keep there
PREVIEWS = tempfile.mkdtemp(prefix="file-sorter-")
atexit.register(shutil.rmtree, PREVIEWS, ignore_errors=True)   # one folder per pass: removed when the pass ends
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
# The files whose text extract() reads go to the local model (no tool: it only answers in SCHEMA), the others to Claude with the Read tool.
# FILE_SORTER_SHADOW=1 (the server's unit from 04/11/2026): Claude still decides for every file, the local model's answers are only compared.
LOCAL_URL = os.environ.get("FILE_SORTER_LOCAL_URL", "http://127.0.0.1:11234")
LOCAL_MODEL = "qwen3.5-9b"   # as the Dashboard's registry of routines names it
SHADOW = os.environ.get("FILE_SORTER_SHADOW") == "1"
# A slot of the local model holds 16,384 tokens: the prompt (the tree, rules.md, convention.md, up to 2,500 characters per file: 500 to
# 1,400 tokens each, figures make them heavier) and the answer (about 100 tokens per file, LOCAL_ANSWER reserved). The batches are measured
# with the model's own tokenizer, LOCAL_FILES files at most.
LOCAL_CONTEXT, LOCAL_MARGIN, LOCAL_ANSWER, LOCAL_FILES, LOCAL_TIMEOUT = 16384, 256, 400, 5, 300
CLAUDE_AI, LOCAL_AI = {"engine": "claude", "model": MODEL}, {"engine": "local", "model": LOCAL_MODEL}   # who decided, for the journal
MIN_AGE = int(os.environ.get("FILE_SORTER_MIN_AGE", "60"))   # seconds without modification before a file is handled
MAX_FILES = 15        # per pass
THRESHOLD = 0.7       # minimum confidence to move a file
DRY = "--dry-run" in sys.argv
# The review of the theme folders (sort.py --decide only): the documents already filed are read again, a few per pass, and each one is kept,
# archived (into the archive folder, at the same path) or sent to the trash, so that the theme folders hold only what is still of use.
# FILE_SORTER_CLEANUP: "trial" (the default) decides and logs proposals, nothing moves; "on" archives and trashes; "off": no review.
CLEANUP = {"on": "on", "off": "off"}.get(os.environ.get("FILE_SORTER_CLEANUP", ""), "trial")   # any other value: the trial, where nothing moves
ARCHIVES = SETTINGS.get("archives") or "90 Archives"   # the archive folder, in iCloud and in the Drive; an existing « NN Archives » wins
ARCHIVE_NAME = re.compile(r"\d\d Archives?", re.I)
THEME = re.compile(r"\d\d ")   # the theme folders are the numbered ones of the root (« 10 Identité & Banque »…), the 00 inboxes aside
REVIEW = os.path.join(DATA, "review.json")
REVIEW_KINDS = ("archiver", "supprimer")   # the plan's actions of the review (the Mac moves into iCloud's archive or to its Trash)
DECISIONS = ("garder", "archiver", "supprimer")
# From REVIEW_LEAVE, a document the model wants out of its theme folder leaves it: to the trash from REVIEW_TRASH, else into the archive,
# where nothing is lost; below REVIEW_LEAVE it stays (the owner's choice of 05/11/2026, ticket 14: with 0.8 to archive and 0.9 to trash,
# four lesser copies of a replaced identity card stayed while their best one went to the archive).
REVIEW_LEAVE, REVIEW_TRASH = 0.7, 0.9
# Nothing of the Drive goes to the trash (the owner's rule of 06/11/2026): what the review would trash there goes into its archive instead.
REVIEW_NO_TRASH = ("drive",)
REVIEW_RULES = 2            # the version of these rules in review.json: a document left in place by former ones is asked again
# The review's state, folder by folder, in the unit's journal (BILAN lines), once per value: how the coding agents, who cannot open
# review.json, read what it holds. A new value logs it again at the next pass.
REVIEW_BILAN = "2026-11-06"
REVIEW_BILAN_LINE = 16000   # characters of JSON per BILAN line at most (journald splits a line past 48K)
REVIEW_EXT = OFFICE + (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff", ".rtf", ".pages", ".numbers", ".key", ".txt")
REVIEW_VISUAL = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif")   # without text, Claude reads these (HEIC through a JPEG)
REVIEW_FILES = 10           # documents asked about per pass, one folder at a time; a folder of 10 or fewer is always read whole
REVIEW_APPLY = 40           # decisions applied per pass at most (once the trial is lifted, the trial's own first)
REVIEW_CHARS = 1200         # of each document's text: enough to tell what it is
REVIEW_LISTING = 150        # names of the folder given as context, at most
REVIEW_MIN_AGE = 3 * 3600   # a document modified in the last three hours is left for later: being written, or just saved (two days until 05/11/2026)
REVIEW_AGAIN = 180 * 86400  # a document is read again after six months: what was current may not be any more
REVIEW_RETRIES = (3600, 86400, 30 * 86400)   # a document left without an answer is asked again an hour later, then a day, then a month
REVIEW_REST = 3600          # once every folder is read, the next walk an hour later at the earliest
REVIEW_PAUSE = 3600         # a model that failed: the review waits an hour, the inboxes do not
# The Dashboard's activity journal and step report (activity.py): written by the server's pass only, the single writer
STEPS = ("inboxes", "decide", "drive", "plan", "icloud")   # the steps of file-sorting's workflow in the home server's registry
if ROLE == "decide":
    activity.PATH = os.path.join(DATA, "activity.jsonl")
REPORT = activity.Report(os.path.join(DATA, "run-status.json") if ROLE == "decide" else None, STEPS)
UNREADABLE = []   # the inboxes that could not be listed during this pass
TEXT_EXT = {".txt", ".csv", ".ics", ".md", ".json", ".html", ".htm", ".xml"}
TEXTUTIL_EXT = {".doc", ".docx", ".rtf", ".odt", ".pages"}
VISION_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
nfc = lambda s: unicodedata.normalize("NFC", s)

# Log lines start with a French keyword (RANGÉ, GARDÉ, DOUBLON, DÉPOSÉ, VERSÉ, LAISSÉ, PLAN, MAC, ERREUR, SANS RÉPONSE, ATTENTE, TROP GROS,
# À DÉPOSER): docs and habits grep the journals for them, so they stay as they are. Notifications stay in French too: the owner reads them.
# So do the actions and texts of the activity journal (« rangé », « renommé », « doublon », « gardé », « à trier »...): the Dashboard shows them.


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


def is_archive(top):
    """A root's first-level folder that is the archive (« 90 Archives »), where the review moves what is no longer current."""
    return bool(ARCHIVE_NAME.fullmatch(nfc(top))) or nfc(top) == nfc(ARCHIVES)


def archive_of(base):
    """The archive folder of a root (iCloud or the Drive): an existing « NN Archives » of its first level, else the settings' name."""
    try:
        found = sorted(d for d in os.listdir(base) if ARCHIVE_NAME.fullmatch(nfc(d)) and os.path.isdir(os.path.join(base, d)))
    except OSError:
        found = []
    return found[0] if found else ARCHIVES


def folders():
    """The destinations offered to the model: iCloud's folders, four levels at most, neither the 00 inboxes nor the archive (the review
    moves there what is no longer current)."""
    out = []
    for r, ds, _ in os.walk(ICLOUD):
        ds[:] = sorted(d for d in ds if not d.startswith("."))
        rel = nfc(os.path.relpath(r, ICLOUD))
        if rel == ".":
            ds[:] = [d for d in ds if not nfc(d).startswith("00 ") and not is_archive(d)]
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
            UNREADABLE.append(where(src))
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
            if (src == DRIVE and not f.lower().endswith(ROOT_DOCUMENTS)) or (src in CHROME_INBOXES and not f.lower().endswith(DOCUMENTS)):
                continue   # not a document: left where it is
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
# One answer of the local model, whose strict schema (local_schema) adds "fichier", one of the names asked: strings of bounded length
FILING_ITEM = {"destination": {"type": "string", "maxLength": 200}, "nouveau_nom": {"type": ["string", "null"], "maxLength": 150},
               "confiance": {"type": "number"}, "raison": {"type": "string", "maxLength": 300}}


class Question:
    """What the models are asked about a batch of files: prompt(blocks, local) builds the question around the files' blocks, schema is
    Claude's answer format and item the properties of one answer in the local model's strict schema, dirs the folders Claude may read,
    read_hint what Claude is told about a document it reads itself (a PDF without text)."""

    def __init__(self, prompt, schema, item, dirs, read_hint=""):
        self.prompt, self.schema, self.item, self.dirs, self.read_hint = prompt, schema, item, dirs, read_hint


CONVENTION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "convention.md")).read()
RULES = open(os.path.join(HERE, "rules.md"), encoding="utf-8").read().strip()


def jpeg_preview(p, jpg):
    """JPEG preview of an image the Read tool cannot read (HEIC, TIFF): sips on the Mac, heif-convert elsewhere."""
    if MAC:
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", p, "--out", jpg], capture_output=True)
    else:
        subprocess.run(["heif-convert", "-q", "85", p, jpg], capture_output=True, timeout=120)


def prompt(tree, blocks, local=False):
    # The prompt stays in French on purpose: it carries the owner's French rules and naming convention, and the names
    # it produces are French. Rewording it would change the names and destinations the model chooses. The local model gets one
    # precision on « confiance »: in the mail sorters' prompts it read the word as how legitimate a document is (03/11/2026).
    confidence = ('- "confiance" : ta certitude sur la destination et le nom que tu proposes, de 0 (au hasard) à 1 (certain). Si tu n\'es pas '
                  'sûr, mets moins de 0.7 : le fichier restera dans "00 À trier".' if local else
                  '- "confiance" entre 0 et 1. Si tu n\'es pas sûr, mets moins de 0.7 : le fichier restera dans "00 À trier".')
    return f"""Tu ranges les documents personnels de {SETTINGS['proprietaire']} dans son iCloud Drive.

Dossiers existants (chemins relatifs, à réutiliser tels quels) :
{chr(10).join('- ' + f for f in tree)}

Règles :
- "destination" = un dossier de la liste ci-dessus. Tu peux ajouter UN niveau de sous-dossier sous un dossier existant si c'est clairement utile (ex. "50 Travail/Auto-entrepreneur/Factures 2026/T4" pour une facture d'octobre 2026 ; trimestres T1=jan-mars, T2=avr-juin, T3=juil-sept, T4=oct-déc).
{RULES}
- "nouveau_nom" : TOUJOURS fourni, conforme à la convention ci-dessous (null uniquement si le nom actuel la respecte déjà exactement).

{CONVENTION}
{confidence}
- "raison" : une phrase courte.
- Le champ "fichier" doit reprendre EXACTEMENT le nom donné après "###". Réponds pour CHAQUE fichier.

Fichiers à classer :

{chr(10).join(blocks)}
"""


def text_block(p, txt):
    return f"### {os.path.basename(p)}\nExtrait du texte :\n{txt}"


def filing(tree):
    """The question of the inboxes: a destination and a name for each file."""
    return Question(lambda blocks, local=False: prompt(tree, blocks, local), SCHEMA, FILING_ITEM,
                    (INBOX, DRIVE_INBOX, DOWNLOADS, GMAIL_INBOX, ATTACH_INBOX, PREVIEWS) + CHROME_INBOXES)   # the Drive's root: through copies


def ask_claude(items, q):
    blocks = []
    for p, txt in items:
        name = os.path.basename(p)
        if txt:
            blocks.append(text_block(p, txt))
        elif os.path.splitext(p)[1].lower() in (".heic", ".heif", ".tiff"):
            jpg = os.path.join(PREVIEWS, hashlib.sha1(p.encode()).hexdigest()[:8] + "-" + re.sub(r"\W", "_", name) + ".jpg")   # one per file
            jpeg_preview(p, jpg)
            blocks.append(f"### {name}\nImage : lis l'aperçu JPEG avec l'outil Read à ce chemin exact : {jpg}")
        else:   # a file of a folder Claude may not read (the Drive's root: it would open the whole Drive) is read through a copy
            path = p if os.path.dirname(p) in q.dirs else os.path.join(PREVIEWS, hashlib.sha1(p.encode()).hexdigest()[:8] + "-" + name)
            if path != p:
                shutil.copy(p, path)
            blocks.append(f"### {name}\nPas de texte extractible : lis-le avec l'outil Read à ce chemin exact : {path}{q.read_hint}")
    dirs = [x for d in q.dirs if os.path.isdir(d) for x in ("--add-dir", d)]
    cmd = [CLAUDE, "-p", q.prompt(blocks), "--model", MODEL, "--output-format", "json",
           "--json-schema", json.dumps(q.schema), "--tools", "Read", "--allowedTools", "Read",
           *dirs, "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir(), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude failed ({r.returncode}): {claude_error(r)}")
    data = json.loads(r.stdout)
    res = data.get("structured_output") or data.get("result")
    if isinstance(res, str):
        res = json.loads(re.search(r"\{.*\}", res, re.S).group(0))
    return res["fichiers"]


def local_call(path, body, timeout=LOCAL_TIMEOUT):
    req = urllib.request.Request(LOCAL_URL.rstrip("/") + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def local_schema(names, item=FILING_ITEM):
    """The answer format for the local model, strict: exactly one answer per file, "fichier" one of the names asked, then the question's own
    properties (item), strings of bounded length."""
    item = {"fichier": {"type": "string", "enum": names}, **item}
    return {"type": "object", "properties": {"fichiers": {"type": "array", "minItems": len(names), "maxItems": len(names), "items": {
        "type": "object", "properties": item, "required": list(item)}}}, "required": ["fichiers"]}


def ask_local(items, q, tag=None):
    """The local model's answers for files with text: ({source: answer}, {source: why it has none, in French}, [the files of each question
    answered]). Batches of LOCAL_FILES files at most, measured with the model's own tokenizer (/tokenize) so that the prompt and its answer
    fit in a slot; a batch it refuses as too long is split. An unreachable model stops the asking (the next batches would wait as long);
    an unreadable answer only loses its batch."""
    if LOCAL_URL in ("", "off"):
        return {}, {p: "FILE_SORTER_LOCAL_URL=off" for p, _ in items}, []
    tag = ("shadow: " if SHADOW else "") if tag is None else tag
    texts, answers, missing, groups = dict(items), {}, {}, []
    try:
        fixed = len(local_call("/tokenize", {"content": q.prompt([], True)}, 60)["tokens"])
        size = {p: len(local_call("/tokenize", {"content": text_block(p, t) + "\n"}, 60)["tokens"]) for p, t in items}
    except Exception as e:
        log(f"{tag}local model unreachable: {type(e).__name__} {str(e)[:150]}")
        return {}, {p: f"injoignable, {type(e).__name__}" for p, _ in items}, []
    room = lambda n: LOCAL_CONTEXT - LOCAL_MARGIN - LOCAL_ANSWER * n - 100   # tokens the prompt may take for n files
    queue, batch = [], []
    for p, _ in items:
        if fixed + size[p] > room(1):
            missing[p] = f"trop long pour un créneau, {fixed + size[p]} tokens"
            continue
        if batch and (len(batch) == LOCAL_FILES or fixed + sum(size[x] for x in batch) + size[p] > room(len(batch) + 1)):
            queue.append(batch)
            batch = []
        batch.append(p)
    queue += [batch] if batch else []
    while queue:
        batch = queue.pop(0)
        names = [os.path.basename(p) for p in batch]
        body = {"model": "local", "temperature": 0, "max_tokens": LOCAL_ANSWER * len(batch) + 100, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": q.prompt([text_block(p, texts[p]) for p in batch], True)}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "fichiers", "schema": local_schema(names, q.item), "strict": True}}}
        try:
            r = local_call("/v1/chat/completions", body)
            choice = r["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("answer cut at max_tokens")
            found = match(json.loads(choice["message"]["content"])["fichiers"], batch)
        except urllib.error.HTTPError as e:
            if e.code == 400 and len(batch) > 1:   # longer than the slot after all (the chat template's share): asked again in two halves
                queue[:0] = [batch[:len(batch) // 2], batch[len(batch) // 2:]]
                continue
            if e.code == 400:
                missing.update({p: "refusé par le modèle local, sans doute trop long" for p in batch})
                continue
            missing.update({p: f"injoignable, HTTP {e.code}" for p in batch + [x for b in queue for x in b]})
            log(f"{tag}local model unavailable: HTTP {e.code}, {len(batch) + sum(map(len, queue))} file(s) without its answer")
            break
        except OSError as e:   # unreachable, refused or too slow: the next batches would fare no better
            missing.update({p: f"injoignable, {type(e).__name__}" for p in batch + [x for b in queue for x in b]})
            log(f"{tag}local model unavailable: {type(e).__name__} {str(e)[:150]}, {len(batch) + sum(map(len, queue))} file(s) without its answer")
            break
        except Exception as e:   # an answer that cannot be read: this batch only
            missing.update({p: f"réponse illisible, {type(e).__name__}" for p in batch})
            log(f"{tag}local model's answer unreadable for {len(batch)} file(s): {type(e).__name__} {str(e)[:150]}")
            continue
        answers.update(found)
        groups.append(batch)
        missing.update({p: "absent de sa réponse" for p in batch if p not in found})
        log(f"{tag}local model {LOCAL_MODEL}: {len(found)} of {len(batch)} file(s) answered, {(r.get('usage') or {}).get('prompt_tokens')} prompt tokens")
    return answers, missing, groups


def claude_error(r):
    """Why claude -p failed: the error and the result of its JSON answer (a usage limit, an API error), else the end of its output."""
    try:
        data = json.loads(r.stdout)
        text = " : ".join(str(data[k]) for k in ("api_error", "result") if data.get(k))
        if text:
            return text
    except (ValueError, TypeError, AttributeError):
        pass
    return (r.stderr[-500:] or r.stdout[-500:]).strip()


def usage_limit(text):
    """« limite d'usage de Claude atteinte, reprise vers 21:30 » when claude -p said so (claude_error), else None."""
    if "usage_limit_reached" not in text:
        return None
    found = re.search(r"resets (\d{1,2})(?::(\d\d))?\s*([ap]m)", text)
    hour = found and int(found.group(1)) % 12 + (12 if found.group(3) == "pm" else 0)
    return "limite d'usage de Claude atteinte" + (f", reprise vers {hour:02d}:{found.group(2) or '00'}" if found else "")


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


def simple(name):
    """A name compared loosely, as a model may write it back (case, accents, punctuation)."""
    return re.sub(r"[^0-9a-z]", "", unicodedata.normalize("NFKD", str(name)).lower())


def named(name, files):
    """The file of a question a model names: its exact name (NFC; the first one when two inboxes hold the same name, the other waits for the
    next pass), else its simplified name (a model may drop an accent) when no other file of the question shares it: « Facture (2).pdf »
    and « Facture 2.pdf » are never taken for each other. None if none or ambiguous."""
    exact = [p for p in files if nfc(os.path.basename(p)) == nfc(str(name))]
    loose = [p for p in files if simple(os.path.basename(p)) == simple(name)]
    return exact[0] if exact else loose[0] if len(loose) == 1 else None


def match(decisions, files):
    """Each answer to its file, by name (named); the first answer for a file wins. -> {source: answer}"""
    found = {}
    for d in decisions if isinstance(decisions, list) else []:
        src = named(d.get("fichier", ""), files) if isinstance(d, dict) else None
        if src and src not in found and os.path.exists(src):
            found[src] = d
    return found


def outcome(src, d, tree):
    """A model's answer for a file, checked: (source, destination or None if the file stays, name, confidence, answer)."""
    name = os.path.basename(src)
    dest = safe_dest(str(d.get("destination") or ""), tree)
    c = d.get("confiance")
    conf = float(c) if type(c) in (int, float) and 0 <= c <= 1 else 0.0
    if not dest or conf < THRESHOLD:
        return src, None, name, conf, d
    new = name
    keeps_name = dest.startswith("80 Images/Photos") or dest.startswith("80 Images/Captures")   # photos and screenshots keep theirs
    if d.get("nouveau_nom") and not keeps_name:
        new = clean_name(str(d["nouveau_nom"]), name)
    if new == name and not keeps_name and os.path.dirname(src) == ATTACH_INBOX:
        # an exported attachment's name never follows the convention: no name proposed means the model meant something else
        # (a copy of another document of the batch, to throw away, which the answer cannot say): the file stays in its inbox
        return src, None, name, conf, {**d, "_copy": True}
    return src, dest, new, conf, d


def described(o):
    """What an answer would do, for a « shadow: » line."""
    _, dest, new, conf, d = o
    return f"{dest}/{new} ({conf:.2f})" if dest else f"stays ({conf:.2f}, proposed « {d.get('destination')} »)"


def compare(texts, claude, local, missing, tree):
    """FILE_SORTER_SHADOW=1: the local model's answer for each file with text next to Claude's, which decides. One « shadow: » line per file
    they would not file the same way (folder or name, after the same checks), then a summary. Nothing else: no action, no event."""
    same = differ = none = 0
    for p, _ in texts:
        if p not in claude:
            continue
        name, h = os.path.basename(p), outcome(p, claude[p], tree)
        if p not in local:
            none += 1
            log(f"shadow: {name}  {MODEL}={described(h)}  local=no answer ({missing.get(p, '?')})")
            continue
        q = outcome(p, local[p], tree)
        if (h[1], nfc(h[2])) == (q[1], nfc(q[2])):
            same += 1
        else:
            differ += 1
            log(f"shadow: {name}  {MODEL}={described(h)}  local={described(q)}")
    if same + differ + none:
        log(f"shadow: {same + differ + none} file(s) with text compared: {same} the same, {differ} different, {none} without a local answer")


def ask(items, q, shadow, tag=None):
    """Asks the models about [(file, its text or None)]: the files with text go to the local model, the others to Claude; with shadow,
    Claude gets them all and the local model's answers are only for comparing. -> (the items with text, the items asked to Claude, the local
    model's answers, why it has none for the others, Claude's answers, Claude's failure or None, [the files of each question answered])."""
    texts = [(p, t) for p, t in items if t]
    to_claude = items if shadow else [(p, t) for p, t in items if not t]
    local, missing, groups = ask_local(texts, q, tag) if texts else ({}, {}, [])
    claude, failure = {}, None
    if to_claude:
        try:
            claude = match(ask_claude(to_claude, q), [p for p, _ in to_claude])
            groups.append([p for p, _ in to_claude])
        except Exception as e:
            failure = e
    return texts, to_claude, local, missing, claude, failure, groups


def choose(files):
    """The decision for each file, validated: ([(source, destination or None if the file stays, name, confidence, answer, who decided)],
    Claude's failure or None, the number of files the local model left without an answer). Files with text go to the local model, the
    others to Claude; with FILE_SORTER_SHADOW=1 Claude decides for every file and the local model is only compared. A file nobody answered
    stays where it is, « sans réponse », and is asked about again at the next pass. When Claude fails, the files it was asked about wait
    too, and the caller fails the pass once the others are done."""
    tree = folders()
    texts, to_claude, local, missing, claude, failure, _ = ask([(p, extract(p)) for p in files], filing(tree), SHADOW)
    if SHADOW:
        if not failure:
            compare(texts, claude, local, missing, tree)
        decided, unanswered = {p: (d, CLAUDE_AI) for p, d in claude.items()}, {}
    else:
        decided = {**{p: (d, LOCAL_AI) for p, d in local.items()}, **{p: (d, CLAUDE_AI) for p, d in claude.items()}}
        unanswered = {p: (LOCAL_AI, why) for p, why in missing.items()}
    if not failure:
        unanswered.update({p: (CLAUDE_AI, None) for p, _ in to_claude if p not in claude})
    for p, (ai, why) in unanswered.items():
        log(f"SANS RÉPONSE {os.path.basename(p)}  ({f'local model: {why}; ' if why else ''}retried on the next pass)")
        activity.log("sans réponse", os.path.basename(p), src=where(os.path.dirname(p)), **ai,
                     detail=(f"pas de réponse du modèle local ({why})" if why else "le modèle n'a rien répondu pour ce fichier") + " : nouvel essai au prochain passage")
    choices = [outcome(p, decided[p][0], tree) + (decided[p][1],) for p in files if p in decided]
    return choices, failure, sum(ai is LOCAL_AI for ai, _ in unanswered.values())


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
            files = pending((DOWNLOADS, GMAIL_INBOX, DRIVE_INBOX) + CHROME_INBOXES + (DRIVE, INBOX, ATTACH_INBOX), skip=lambda p: p in done)
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
    choices, failure, _ = choose(files)
    for src, dest, new, conf, d, _ai in choices:
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
    if failure:   # Claude failed for the files it was asked about: they wait, the pass reports the error
        raise failure


# ---- decision on the server, move on the Mac ----
# plan.json and results.json are exchanged between the server and the Mac: their French keys
# (actions, faites, appliques, racine, chemin, taille, cible, cree_le...), action types and statuses are a contract.

def relative(p):
    """(root, path relative to it in NFC) of a file in iCloud or the Drive ("." for a root itself)."""
    return ("drive", nfc(os.path.relpath(p, DRIVE))) if p == DRIVE or p.startswith(DRIVE + os.sep) else ("icloud", nfc(os.path.relpath(p, ICLOUD)))


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


# ---- the activity journal and the step report (activity.py): what the server decided and did, and what the Mac did ----

def place(root, rel=""):
    """A place as the owner reads it: "Google Drive/…" or "iCloud/…" ("Google Drive" alone for its root)."""
    return f"{'Google Drive' if root == 'drive' else 'iCloud'}/{'' if rel == '.' else rel}".rstrip("/")


def where(p):
    """The place of a path inside iCloud or the Drive."""
    return place(*relative(p))


def twin(orig):
    """The original of a duplicate, which dedupe gives relative to iCloud, as a place."""
    return where(os.path.normpath(os.path.join(ICLOUD, orig)))


def kept_why(conf, d):
    """Why a file stays where it is, for the journal."""
    if d.get("_copy"):
        why = "pièce jointe sans nouveau nom proposé : sans doute la copie d'un autre document"
    elif conf < THRESHOLD:
        why = f"pas assez sûr ({round(conf * 100)} %, il faut {round(THRESHOLD * 100)} %)"
    else:
        why = "dossier proposé hors de l'arborescence"
    proposed, reason = d.get("destination"), str(d.get("raison") or "").strip().rstrip(".")
    return f"{why} ; proposait « {proposed} »" + (f" : {reason}" if reason else "") if proposed else why


def filed(name, origin, target, d, ai, how):
    """The events of a filing: « rangé », then « renommé » when the name changes. target: the new place, name included."""
    reason = str(d.get("raison") or "").strip().rstrip(".")   # the model's sentence, joined to what happens next
    events = [("rangé", name, {"src": origin, "dst": target, "detail": " ; ".join(x for x in (reason, how) if x), **ai})]
    new = target.rsplit("/", 1)[-1]
    if nfc(new) != nfc(name):
        events.append(("renommé", name, {"src": name, "dst": new, "detail": "nom refait selon la convention", **ai}))
    return events


MAC_DONE = {"ranger": "rangé par le Mac", "doublon": "doublon jeté par le Mac", "vers_a_trier": "mis à trier par le Mac", "deposer": "déposé par le Mac",
            "archiver": "archivé par le Mac", "supprimer": "supprimé par le Mac"}
MAC_VERB = {"ranger": "le ranger", "doublon": "le mettre à la Corbeille", "vers_a_trier": "le mettre dans 00 À trier", "deposer": "l'écrire",
            "archiver": "l'archiver", "supprimer": "le mettre à la Corbeille"}
MAC_FINE = ("ok", "absent", "modifié depuis la décision")   # the Mac's statuses that are no failure
DRIVE_WHAT = {"doublon": "mise à la corbeille du Drive", "vers 00 À trier": "déplacement vers 00 À trier", "rangement": "rangement dans le Drive",
              "archivage": "archivage dans le Drive", "suppression": "mise à la corbeille du Drive"}


DOWNLOADS_LOGGED = os.path.join(DATA, "downloads-logged.json")   # server: ids of the Mac's downloads already in the journal


def downloads_events(entries):
    """Server: the journal's events for what the Mac did with its Downloads (results.json), each once."""
    logged = {k: v for k, v in read_json(DOWNLOADS_LOGGED, {}).items() if time.time() - v < 8 * 86400}
    events = []
    for x in entries:
        try:
            if x["id"] in logged:
                continue
            logged[x["id"]] = time.time()
            events.append((x["action"], x["nom"], {"src": "Mac · Téléchargements", "dst": x.get("vers") or "Téléchargements",
                                                   "detail": x.get("detail") or ""}))
        except (KeyError, TypeError):   # an entry from another version of the Mac's side: skipped
            continue
    if events:
        write_json(DOWNLOADS_LOGGED, logged)
    return events


def mac_event(a, status):
    """The journal's event for an action of the plan the Mac reports on in results.json: (action, object, fields)."""
    try:
        return _mac_event(a, str(status))
    except Exception:   # a malformed action or status must not stop the pass
        return "fait par le Mac", str(a.get("id", "?")), {"detail": str(status)}


def _mac_event(a, status):
    kind, src, target = a.get("type"), a.get("source") or {}, a.get("cible")
    if kind == "deposer":
        obj, fields = os.path.basename(target["chemin"]), {"src": "serveur", "dst": place(target["racine"], target["chemin"])}
    else:
        obj = os.path.basename(src.get("chemin", ""))
        fields = {"src": place(src.get("racine"), os.path.dirname(src.get("chemin", ""))),
                  "dst": place(target["racine"], os.path.dirname(target["chemin"]) if kind == "archiver" else target["chemin"]) if target
                  else "Corbeille du Mac" if kind in ("doublon", "supprimer") else where(DRIVE_INBOX)}
    if status == "ok":
        return MAC_DONE.get(kind, "fait par le Mac"), obj, {**fields, "detail": f"identique à {twin(a['original'])}" if a.get("original")
                                                             else a.get("raison") if kind in REVIEW_KINDS else None}
    if status in MAC_FINE:
        return "annulé", obj, {**fields, "detail": "le Mac ne l'a plus trouvé : déplacé ou supprimé entre-temps" if status == "absent"
                               else "modifié depuis la décision : il sera examiné de nouveau"}
    error = status.removeprefix("erreur : ")
    return "échec", obj, {**fields, "detail": f"le Mac n'a pas pu {MAC_VERB.get(kind, 'appliquer la décision')} : {error}"}


def public_reason(e, step):
    """A failure as the step report shows it: French, without a file name (the journal has the details)."""
    try:
        return _public_reason(e, step)
    except Exception:
        return f"erreur {type(e).__name__}"


def _public_reason(e, step):
    if isinstance(e, subprocess.TimeoutExpired):
        program = os.path.basename(str(e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd))
        return f"{program} n'a pas répondu en {int(e.timeout // 60)} min" if e.timeout >= 120 else f"{program} n'a pas répondu en {int(e.timeout)} s"
    limit = usage_limit(str(e))
    if limit:
        return limit
    found = re.match(r"claude failed \((-?\d+)\)(?:: ([a-z_]+) : )?", str(e))
    if found:
        return f"Claude a échoué (code {found.group(1)}{', ' + found.group(2) if found.group(2) else ''})"
    if step == "decide" and isinstance(e, (ValueError, KeyError, TypeError, AttributeError)):
        return "réponse de Claude illisible"
    if isinstance(e, OSError):
        return f"erreur système : {e.strerror or type(e).__name__}"
    return f"erreur {type(e).__name__}"


# ---- the review of the theme folders: each document already filed is kept, archived or trashed (--decide only) ----
# Its state, review.json in FILE_SORTER_DATA, is the server's own (English keys): "files", each document read, by "root|path|size" (path
# relative to the root, NFC): {"path": its real relative path, "decision", "confidence", "reason", "twin": the real path of the copy kept,
# "engine", "model", "proposal": the model's own decision (since 05/11/2026 evening), "time", "state": kept | pending (to archive or trash:
# a proposal of the trial, or waiting for the Mac) | done | failed | unanswered, "tries": how many questions left it without an answer};
# "cursor": the last folder read entirely; "rest_until", "pause_until"; "sweep": the counts of the walk under way; "first_sweep": when the
# first walk over every folder ended (its end is notified once); "withdrawn": {action id: {"action", "time"}}, the plan's actions taken back
# when the review stopped, a week long; "rules": the version of the rules the decisions were taken under (REVIEW_RULES; none: the first);
# "bilan": the REVIEW_BILAN last logged.

# The review's answer format. The keys are French, like the filing's: "raison" comes first, so that the model says why before it decides.
REVIEW_SCHEMA = {"type": "object", "required": ["fichiers"], "properties": {"fichiers": {"type": "array", "items": {
    "type": "object", "required": ["fichier", "raison", "decision", "doublon_de", "confiance"],
    "properties": {"fichier": {"type": "string"}, "raison": {"type": "string"}, "decision": {"type": "string", "enum": list(DECISIONS)},
                   "doublon_de": {"type": ["string", "null"]}, "confiance": {"type": "number"}}}}}}
REVIEW_ITEM = {"raison": {"type": "string", "maxLength": 300}, "decision": {"type": "string", "enum": list(DECISIONS)},
               "doublon_de": {"type": ["string", "null"], "maxLength": 200}, "confiance": {"type": "number"}}
CODE_MARKERS = {"package.json", "composer.json", "pom.xml", ".git", "angular.json", "src", "app", "vendor", "node_modules"}   # as audit.py
# a reason that speaks of a copy or a version (broadly: a word too many only keeps a document): then the kept one must be known, named in
# doublon_de or, by its full name, in the reason itself
COPY_WORDS = re.compile(r"\b(copie|doublon|double|duplicata|identique|exemplaire|version|réplique|déjà|autre fichier|même|qualité|"
                        r"moins (bonne?|nette?|lisible|complète?)|plus (floue?|nette?|lisible|complète?|récente?))", re.I)
PHOTOS = "80 Images"   # photos and screenshots: never reviewed (they keep their names too, see outcome)


def review_prompt(root, folder, files, archive, blocks, local=False):
    # French on purpose, like the filing's prompt: the owner's documents, folders and rules are French. What is current, what is kept as a
    # proof and what is of no use are the owner's words (ticket 14 of 05/11/2026: temporary files, several identity cards, mail
    # attachments, terms of sale); his rules.md comes after and may add his own.
    # The same scale as before 05/11/2026 evening, so that the models' certainty means what it meant; what the script does with it (REVIEW_LEAVE,
    # REVIEW_TRASH) is not theirs to weigh.
    confidence = ('- "confiance" : ta certitude sur cette décision, de 0 (au hasard) à 1 (certain). Si tu n\'es pas sûr, mets moins de 0.8.' if local else
                  '- "confiance" entre 0 et 1. Si tu n\'es pas sûr, mets moins de 0.8.')
    return f"""Tu fais le ménage dans les documents personnels de {SETTINGS['proprietaire']}, rangés par thèmes dans son {'Google Drive' if root == 'drive' else 'iCloud Drive'}. Nous sommes le {datetime.now():%d/%m/%Y}.
Les dossiers par thèmes ne gardent que ce qui sert encore ; le reste part dans « {archive} » (au même chemin) ou à la corbeille.

Dossier relu : « {folder} ». Tous ses fichiers, pour repérer les doublons et les versions plus récentes :
{files}

Pour chaque fichier examiné plus bas, "decision" vaut :
- "garder" : encore d'actualité ou utile : pièce d'identité ou titre encore valable, contrat, bail, assurance, prêt ou abonnement en cours, diplôme, acte d'état civil, document médical utile, garantie d'un bien encore possédé ; facture, bulletin de paie, avis d'impôt, relevé ou déclaration de l'année en cours ou des deux précédentes. Dans le doute, garder.
- "archiver" : ne sert plus au quotidien mais peut encore servir de preuve : pièce périmée ou remplacée par une plus récente, contrat, bail ou abonnement terminé, ancien logement ou ancien employeur, dossier clos ; facture, bulletin de paie, avis d'impôt, relevé ou déclaration plus anciens.
- "supprimer" (corbeille) : sans intérêt : conditions générales (de vente, d'utilisation), notice, mentions légales, politique de confidentialité, publicité, newsletter, document générique qui ne le concerne pas personnellement ; fichier temporaire dont l'usage est passé (pré-demande, convocation, rendez-vous, billet ou réservation passés, code, QR code, brouillon) ; ou copie moins bonne d'un autre fichier examiné plus bas (le même document : scan plus flou, photo d'un document déjà scanné, version incomplète).
- Jamais "supprimer" une facture, un justificatif comptable ou fiscal, un bulletin de paie, un contrat signé, une pièce d'identité ou un acte, sauf la copie moins bonne d'un autre fichier examiné : archive-les plutôt.
- Plusieurs versions d'un même document : garder la meilleure (la plus récente encore valable, complète et lisible), archiver les anciennes, supprimer les copies moins bonnes. Le recto et le verso d'un même document ne sont pas des doublons.
- "doublon_de" : OBLIGATOIRE pour une copie moins bonne à supprimer : le nom exact du fichier examiné qu'on garde à sa place ; sinon null.
Règles du propriétaire :
{RULES}
{confidence}
- "raison" : une phrase courte.
- Le champ "fichier" doit reprendre EXACTEMENT le nom donné après "###". Réponds pour CHAQUE fichier.

Fichiers à examiner :

{chr(10).join(blocks)}
"""


def theme_walk(cursor):
    """The theme folders of iCloud, then of the Drive, in order, after the cursor (the position of the last one read entirely): (position,
    root, base, folder, its path relative to the root in NFC, its file names). Only the numbered folders of each root and what they hold:
    neither the 00 inboxes, the archive, the photos and the settings' bundles, nor a code project. The order is that of the paths' parts,
    so that what comes before the cursor is skipped without being listed."""
    bundles = tuple(SETTINGS.get("paquets") or ()) + (PHOTOS,)
    for i, (root, base) in enumerate((("icloud", ICLOUD), ("drive", DRIVE))):
        if (cursor and i < cursor[0]) or not os.path.isdir(base):
            continue
        for r, ds, fs in os.walk(base):
            rel = nfc(os.path.relpath(r, base))
            parts = () if rel == "." else tuple(rel.split(os.sep))
            here = (i,) + parts
            if parts and CODE_MARKERS & set(ds + fs):
                ds[:] = []
                continue
            kept = []
            for d in sorted(ds, key=nfc):
                n = nfc(d)
                path = parts + (n,)
                if n.startswith(".") or any(b in "/" + "/".join(path) for b in bundles):
                    continue
                if not parts and (not THEME.match(n) or n.startswith("00 ") or is_archive(n)):
                    continue
                if cursor and (i,) + path < cursor and cursor[:len(path) + 1] != (i,) + path:
                    continue   # wholly before the cursor: read in an earlier pass
                kept.append(d)
            ds[:] = kept
            if parts and not (cursor and here <= cursor):
                yield here, root, base, r, rel, fs


def exact_copies(docs):
    """The documents of a folder identical to another one (same size, same SHA-1): {copy: the one kept}, which has the best name (no « (2) »,
    a date first, the shortest)."""
    by_size = {}
    for p in docs:
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size:
            by_size.setdefault(size, []).append(p)
    rank = lambda p: (bool(re.search(r"\(\d+\)", os.path.basename(p))), not re.match(r"\d{4}-\d\d-\d\d ", os.path.basename(p)),
                      len(os.path.basename(p)), os.path.basename(p))
    out = {}
    for same in (s for s in by_size.values() if len(s) > 1):
        by_hash = {}
        for p in same:
            try:
                by_hash.setdefault(sha(p), []).append(p)
            except OSError:
                continue
        for group in (g for g in by_hash.values() if len(g) > 1):
            kept = min(group, key=rank)
            out.update({p: kept for p in group if p != kept})
    return out


def listing(docs, chunk):
    """The folder's documents as the question lists them, name and size: REVIEW_LISTING at most, around those asked about (docs sorted,
    chunk among them)."""
    shown = docs
    if len(docs) > REVIEW_LISTING:
        first = min(docs.index(p) for p in chunk)
        start = max(0, min(first - REVIEW_LISTING // 4, len(docs) - REVIEW_LISTING))
        shown = docs[start:start + REVIEW_LISTING]
        shown += [p for p in chunk if p not in shown]
    lines = [f"- {os.path.basename(p)} ({max(1, round(os.path.getsize(p) / 1024))} Ko)" for p in shown]
    return "\n".join(lines + ([f"- … et {len(docs) - len(shown)} autre(s) fichier(s)"] if len(shown) < len(docs) else []))


def judge(decided, groups, copies):
    """The review's decisions once checked: {document: (decision, confidence, the copy kept in its place or None, reason, who decided,
    the decision proposed when it was not followed, the model's own decision)}. An unknown decision, or one below REVIEW_LEAVE, keeps the
    document. A copy (named so in doublon_de, or by its full name in its reason) leaves only when the one kept was in the same question (the
    model saw both), is named unambiguously, and does not leave as a copy itself; the exact copies (copies, same bytes) always do. Last, the
    trash needs REVIEW_TRASH: a document the model wants to trash with less certainty goes to the archive instead."""
    together = {p: g for g in groups for p in g}
    order = sorted(decided, key=lambda p: nfc(os.path.basename(p)).lower())
    out = {}
    for p in order:
        d, ai = decided[p]
        decision = wanted = d.get("decision") if d.get("decision") in DECISIONS else "garder"
        c = d.get("confiance")
        conf = float(c) if type(c) in (int, float) and 0 <= c <= 1 else 0.0
        reason, kept, proposed = str(d.get("raison") or "").strip().rstrip("."), None, None
        if decision != "garder" and conf < REVIEW_LEAVE:
            decision, proposed = "garder", f"{decision}, pas assez sûr"
        others, cited = [x for x in together.get(p, ()) if x != p], []
        if decision == "supprimer" and d.get("doublon_de"):
            kept = named(d["doublon_de"], others)
            if not kept:
                decision, proposed = "garder", "supprimer comme copie d'un fichier qu'il n'a pas vu"
        elif decision == "supprimer":   # without doublon_de: the files of the question its reason names, by their full names
            cited = sorted((x for x in others if nfc(os.path.basename(x)).lower() in nfc(reason).lower()), key=lambda x: -len(os.path.basename(x)))
            if COPY_WORDS.search(reason):   # a copy: the one it names is the one kept, else it stays
                kept = cited[0] if len(cited) == 1 or (cited and len(os.path.basename(cited[0])) > len(os.path.basename(cited[1]))) else None
                if not kept:
                    decision, proposed = "garder", "supprimer comme copie, sans nommer le fichier gardé"
        out[p] = [decision, conf, kept, reason, ai, proposed, wanted, cited]
    for p in order:   # a document leaving while the one it names (its kept copy, or a file its reason cites) leaves too that way stays
        v = out[p]
        if v[0] == "supprimer" and any(out.get(x, ["garder"])[0] == "supprimer" for x in ([v[2]] if v[2] else v[7])):
            v[0], v[2], v[5] = "garder", None, "supprimer, mais l'autre exemplaire part aussi"
    for v in out.values():   # wanted out, not certain enough for the trash: into the archive, where nothing is lost
        if v[0] == "supprimer" and v[1] < REVIEW_TRASH:
            v[0], v[5] = "archiver", "supprimer, pas assez sûr pour la corbeille"
    for p, kept in copies.items():
        out[p] = ["supprimer", 1.0, kept, f"copie identique de « {os.path.basename(kept)} »", None, None, None]
    return {p: tuple(v[:7]) for p, v in out.items()}


class Review:
    """The review of the theme folders within a --decide pass (FILE_SORTER_CLEANUP trial or on). The pass gives what it shares: the plan's
    actions, the events logged once the plan is written (later), then the sources already in the plan (planned) and by_server for a move
    in the Drive. Its actions taken out of the plan (withdrawn: the review stopped or back to its trial) are remembered a week, so that
    what the Mac had already fetched and does anyway is still logged."""

    def __init__(self, actions, later):
        self.now = time.time()
        state, broken = read_json(REVIEW, {}), False
        if not isinstance(state, dict) or not isinstance(state.get("files", {}), dict):
            log("review: review.json unreadable, the review starts again from the first folder")
            state, broken = {}, True
        state["files"] = {k: e for k, e in (state.get("files") or {}).items() if isinstance(e, dict) and isinstance(e.get("path"), str) and k.count("|") >= 2}
        cursor = state.get("cursor")
        if not (isinstance(cursor, list) and cursor and type(cursor[0]) is int and all(isinstance(x, str) for x in cursor[1:])):
            state["cursor"] = None
        state["withdrawn"] = {k: w for k, w in (state.get("withdrawn") or {}).items()
                              if isinstance(w, dict) and isinstance(w.get("action"), dict) and w["action"].get("id") == k}
        migrated = state.get("rules", 1) < REVIEW_RULES
        if migrated:
            # A folder where a document was kept under former rules with a certainty the present ones act on (from REVIEW_LEAVE, below
            # REVIEW_TRASH) is read again whole, at once: review.json did not keep what the model had proposed, and a copy leaves only when
            # the one kept in its place is in the same question. Its decisions stand until then (a proposal already in the journal is not
            # logged twice); the walk starts over from the first folder, a folder with nothing due being passed in the same pass.
            where = lambda k, e: (k.split("|", 1)[0], nfc(os.path.dirname(e["path"])))
            again = {where(k, e) for k, e in state["files"].items() if e.get("decision") == "garder" and e.get("state") == "kept" and e.get("engine")
                     and type(e.get("confidence")) in (int, float) and REVIEW_LEAVE <= e["confidence"] < REVIEW_TRASH}
            due = [e for k, e in state["files"].items() if where(k, e) in again]
            for e in due:
                e["time"] = 0
            if again:
                state["cursor"] = None
                state.pop("rest_until", None)
                log(f"review: new rules, {len(again)} folder(s) read again whole ({len(due)} document(s)), where documents stayed for lack of certainty")
            state["rules"] = REVIEW_RULES
        # Nothing of the Drive goes to the trash (06/11/2026): what the trial proposed to trash there goes into its archive, where nothing is lost
        spared = [e for k, e in state["files"].items() if k.split("|", 1)[0] in REVIEW_NO_TRASH and e.get("decision") == "supprimer"
                  and e.get("state") == "pending"]
        for e in spared:
            e.update(decision="archiver", proposal="supprimer")
        if spared:
            log(f"review: {len(spared)} decision(s) to trash in the Drive go into its archive instead: nothing of the Drive is trashed")
            later.append(("revu", place("drive"), {"detail": f"{len(spared)} document(s) proposé(s) pour la corbeille partent plutôt aux archives : "
                                                             "rien n'est supprimé dans le Drive"}))
        self.state, self.files, self.withdrawn = state, state["files"], state["withdrawn"]
        # a broken, migrated or spared one is written again at once
        self.read_as = None if broken or migrated or spared else json.dumps(state, sort_keys=True)
        self.actions, self.later, self.planned, self.by_server = actions, later, set(), None
        self.roots, self.index = {}, None   # which roots show their theme folders; the entries by folder (both once per pass)

    def changed(self):
        return json.dumps(self.state, sort_keys=True) != self.read_as

    def save(self):
        write_json(REVIEW, self.state)

    def returned(self, a, status):
        """What the Mac did with an action of the review: done, forgotten (gone or changed: read again where it is) or failed."""
        key = f"{a['source']['racine']}|{nfc(a['source']['chemin'])}|{a['taille']}"
        if key not in self.files:
            return
        if status == "ok":
            self.files[key].update(state="done", time=self.now)
        elif status in MAC_FINE:
            del self.files[key]
        else:
            self.files[key].update(state="failed", time=self.now)

    def due(self, key):
        """A document to read: never read, read six months ago, or left without an answer (asked again an hour later, then a day, then a month,
        so that a document a model always refuses never holds the walk back)."""
        e = self.files.get(key)
        if not e:
            return True
        wait = REVIEW_RETRIES[min(max(e.get("tries", 1), 1), len(REVIEW_RETRIES)) - 1] if e.get("state") == "unanswered" else REVIEW_AGAIN
        return self.now - e.get("time", 0) >= wait

    def why(self, e, root=None):
        """A decision's reason, as the journal says it; for a document the model wanted to trash, why it goes to the archive instead."""
        reason = e.get("reason") or ""
        if e.get("twin") and not reason.startswith("copie identique"):
            reason = f"copie moins bonne de « {os.path.basename(e['twin'])} »" + (f" : {reason}" if reason else "")
        if e.get("decision") == "archiver" and e.get("proposal") == "supprimer":
            reason = (reason or "décision du modèle") + (" ; à l'archive plutôt qu'à la corbeille : rien n'est supprimé dans le Drive"
                                                         if root in REVIEW_NO_TRASH else " ; à l'archive plutôt qu'à la corbeille, faute de certitude")
        return reason or "décision du modèle"

    def mounted(self, root):
        """A root shows its theme folders: a mount that is down or empty (iCloud waiting for its monthly reconnection) shows none, and then
        the review neither forgets nor carries out anything there: every document would look gone."""
        if root not in self.roots:
            try:
                base = DRIVE if root == "drive" else ICLOUD
                self.roots[root] = any(THEME.match(nfc(d)) and not nfc(d).startswith("00 ") for d in os.listdir(base))
            except OSError:
                self.roots[root] = False
        return self.roots[root]

    def apply_pending(self):
        """FILE_SORTER_CLEANUP=on: the decisions waiting (the trial's proposals first, a Drive move that failed) carried out, REVIEW_APPLY at most."""
        waiting = sorted(((k, e) for k, e in self.files.items() if e.get("state") == "pending" and self.mounted(k.split("|", 1)[0])),
                         key=lambda x: (x[1]["decision"] != "archiver", x[1].get("time", 0)))
        done = 0
        for key, e in waiting:
            if done >= REVIEW_APPLY:
                break
            done += self.apply(key, e)

    def apply(self, key, e):
        """One decision to archive or trash carried out: in the Drive by the server (FILE_SORTER_DRIVE_RCLONE), in iCloud through the Mac's plan.
        True when something was done or planned."""
        root = key.split("|", 1)[0]
        base = DRIVE if root == "drive" else ICLOUD
        p, rel = os.path.join(base, e["path"]), nfc(e["path"])
        if root in REVIEW_NO_TRASH and e["decision"] == "supprimer":   # never the Drive's trash: its archive
            e.update(decision="archiver", proposal="supprimer")
        if f"{root}|{rel}" in self.planned:
            return False   # already in the plan, waiting for the Mac
        if not os.path.isfile(p) or str(os.path.getsize(p)) != key.rsplit("|", 1)[1]:
            del self.files[key]   # moved, removed or changed since the decision: read again wherever it is now
            return False
        archive, name, folder = archive_of(base), os.path.basename(p), os.path.dirname(e["path"])
        if e["decision"] == "supprimer" and e.get("twin") and not any(os.path.exists(os.path.join(base, *x, e["twin"])) for x in ((), (archive,))):
            e.update(state="failed", time=self.now)
            log(f"ERREUR  {name} not trashed: the copy kept in its place is gone ({e['twin']})")
            return False
        why, ai = self.why(e, root), {k: e[k] for k in ("engine", "model", "confidence") if e.get(k) is not None}
        origin = place(root, nfc(folder))
        if root == "drive" and DRIVE_RCLONE:
            if e["decision"] == "archiver":
                target = self.by_server(p, "archivage", lambda: drive_move(p, os.path.join(archive, folder), name))
                renamed = target and nfc(os.path.basename(target)) != nfc(name)
                event = ("archivé", {"dst": place("drive", nfc(os.path.dirname(target or ""))), "detail": f"{why} ; archivé par le serveur dans le Drive"
                                     + (f", sous le nom « {os.path.basename(target)} » : le sien était pris" if renamed else "")})
            else:
                target = self.by_server(p, "suppression", lambda: rclone("deletefile", "--drive-use-trash", DRIVE_RCLONE + os.path.relpath(p, DRIVE)) or "corbeille du Drive")
                event = ("supprimé", {"dst": "corbeille du Drive", "detail": f"{why} ; mis à la corbeille du Drive par le serveur"})
            if not target:   # by_server logged the failure: tried again at the next passes, three times
                e["tries"] = e.get("tries", 0) + 1
                if e["tries"] >= 3:
                    e.update(state="failed", time=self.now)
                return True
            e.update(state="done", time=self.now)
            activity.log(event[0], name, src=origin, **event[1], **ai)
            log(f"{'ARCHIVÉ' if e['decision'] == 'archiver' else 'SUPPRIMÉ':8} {name}  ->  {event[1]['dst']} (by the server) — {why}")
            return True
        if e["decision"] == "archiver":
            target = {"racine": root, "chemin": nfc(os.path.join(archive, e["path"]))}
            self.actions.append(make_action("archiver", p, cible=target, raison=why))
            self.later.append(("archivé", name, {"src": origin, "dst": place(root, os.path.dirname(target["chemin"])), "detail": f"{why} ; le Mac l'archive à son prochain passage", **ai}))
        else:   # the kept copy goes with it: the Mac checks it is still there before trashing this one
            self.actions.append(make_action("supprimer", p, raison=why, **({"garde": {"racine": root, "chemin": nfc(e["twin"])}} if e.get("twin") else {})))
            self.later.append(("supprimé", name, {"src": origin, "dst": "Corbeille du Mac", "detail": f"{why} ; le Mac le met à la Corbeille à son prochain passage", **ai}))
        self.planned.add(f"{root}|{rel}")
        log(f"PLAN    {name}  ->  {e['decision']} — {why}")
        return True

    def step(self):
        """One folder's documents read and decided, REVIEW_FILES at most, from the cursor on. True when a model was asked."""
        if self.now < max(self.state.get("rest_until", 0), self.state.get("pause_until", 0)):
            return False
        cursor = tuple(self.state.get("cursor") or ())
        for here, root, base, r, rel, names in theme_walk(cursor):
            found = self.folder(root, base, r, rel, names)
            if found:
                return self.read(here, root, base, r, rel, *found)
            self.state["cursor"] = list(here)   # nothing to read there: the next pass starts after it
        self.walk_over()
        return False

    def folder(self, root, base, r, rel, names):
        """A folder's documents to read: (the documents it keeps, to list in the question, those old enough to be read, both sorted, their
        keys, the ones to ask about, all read after them) or None. Its documents are read together when they are REVIEW_FILES or fewer, so
        that a new copy meets the former ones; never one already archived or trashed (the mount shows it a few minutes more) nor one whose
        action waits for the Mac."""
        if self.index is None:
            self.index = {}
            for k, e in self.files.items():
                self.index.setdefault((k.split("|", 1)[0], nfc(os.path.dirname(e.get("path") or ""))), set()).add(k)
        keys, listed, docs = {}, [], []
        for f in names:
            p = os.path.join(r, f)
            if f.startswith(".") or not f.lower().endswith(REVIEW_EXT):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            keys[p] = key = f"{root}|{nfc(os.path.relpath(p, base))}|{st.st_size}"
            if self.files.get(key, {}).get("state") == "done" or f"{root}|{nfc(os.path.relpath(p, base))}" in self.planned:
                continue
            listed.append(p)
            if self.now - st.st_mtime >= REVIEW_MIN_AGE:   # its modification only: a document filed keeps its date, an edited one gets a new one
                docs.append(p)
        for k in self.index.get((root, rel), set()) - set(keys.values()):
            self.files.pop(k, None)   # no longer there
        order = lambda p: nfc(os.path.basename(p)).lower()
        listed.sort(key=order)
        docs.sort(key=order)
        todo = [p for p in docs if self.due(keys[p])]
        if not todo:
            return None
        # a document a model already left twice without an answer is asked again alone: its folder was read with it already
        alone = all((self.files.get(keys[p]) or {}).get("state") == "unanswered" and self.files[keys[p]].get("tries", 1) >= 2 for p in todo)
        chunk = todo[:1] if alone else docs if len(docs) <= REVIEW_FILES else todo[:REVIEW_FILES]   # alone: one a pass, a failure isolated
        return listed, docs, keys, chunk, set(todo) <= set(chunk)

    def read(self, here, root, base, r, rel, listed, docs, keys, chunk, complete):
        """Asks about the chunk, records and carries out (on) or proposes (trial) the decisions, sums them up in one « revu »."""
        copies = {p: k for p, k in exact_copies(docs).items() if p in chunk}
        asked = [p for p in chunk if p not in copies]
        archive = archive_of(base)
        text = listing(listed, chunk)
        q = Question(lambda blocks, local=False: review_prompt(root, rel, text, archive, blocks, local), REVIEW_SCHEMA, REVIEW_ITEM, (r, PREVIEWS),
                     " (la première page suffit)")
        REPORT.running("decide")
        items = [(p, (lambda t: t[:REVIEW_CHARS] if t else None)(extract(p))) for p in asked]
        unreadable = [p for p, t in items if not t and not p.lower().endswith(REVIEW_VISUAL)]   # neither text nor what Claude's Read shows
        items = [(p, t) for p, t in items if p not in unreadable]
        _, to_claude, local, missing, claude, failure, groups = ask(items, q, False, "review: ") if items else ([], [], {}, {}, {}, None, [])
        verdicts = judge({**{p: (d, LOCAL_AI) for p, d in local.items()}, **{p: (d, CLAUDE_AI) for p, d in claude.items()}}, groups, copies)
        verdicts.update({p: ("garder", 0.0, None, "format que la relecture ne lit pas", None, None, None) for p in unreadable})
        if root in REVIEW_NO_TRASH:   # nothing of the Drive goes to the trash (06/11/2026): into its archive, where nothing is lost
            verdicts = {p: ("archiver", *v[1:5], "supprimer, jamais de corbeille dans le Drive", v[6] or "supprimer") if v[0] == "supprimer" else v
                        for p, v in verdicts.items()}
        to_claude = {p for p, _ in to_claude}
        unanswered = [p for p in asked if p not in verdicts]
        out_of_reach = failure is not None or any(missing.get(p, "").startswith("injoignable") for p in unanswered)
        retried = []   # those left without an answer that will be asked again
        input_error = failure is not None and re.search(r"invalid_request|could not process|too (long|large)|exceed", str(failure), re.I)
        for p in unanswered:   # asked again an hour later, then a day, then a month (due)
            prev = self.files.get(keys[p]) or {}
            if prev and prev.get("state") != "unanswered" and not self.due(keys[p]):
                continue   # read again only with its folder: its former decision stands
            retried.append(p)
            why = missing.get(p) or (f"Claude : {public_reason(failure, 'decide')}" if failure and p in to_claude else "absent de sa réponse")
            # a model out of reach (unreachable, its usage limit) is no fault of the document: only what failed for this question counts
            own = not (missing.get(p, "").startswith("injoignable") or (failure is not None and p in to_claude and not input_error))
            self.files[keys[p]] = {"path": os.path.relpath(p, base), "state": "unanswered", "time": self.now, "reason": why,
                                   "tries": (prev.get("tries", 0) if prev.get("state") == "unanswered" else 0) + own}
            log(f"SANS RÉPONSE {os.path.basename(p)}  (review: {why}; try {self.files[keys[p]]['tries']})")
        trial, counts, start = CLEANUP != "on", {d: 0 for d in DECISIONS}, len(self.later)
        for p, (decision, conf, kept, reason, ai, proposed, wanted) in verdicts.items():
            counts[decision] += 1
            prev = self.files.get(keys[p]) or {}
            e = self.files[keys[p]] = {"path": os.path.relpath(p, base), "decision": decision, "confidence": round(conf, 3), "reason": reason,
                                       "twin": kept and os.path.relpath(kept, base), **(ai or {}), **({"proposal": wanted} if wanted else {}),
                                       "time": self.now, "state": "kept" if decision == "garder" else "pending"}
            log(f"RELU    {os.path.basename(p)}  {decision} ({conf:.2f}{', ' + proposed if proposed else ''}) — {self.why(e, root)}")
            if decision == "garder":
                continue
            if not trial:
                self.apply(keys[p], e)
                continue
            if prev.get("state") == "pending" and (prev.get("decision"), prev.get("twin")) == (decision, e["twin"]):
                continue   # read again with its folder, the same proposal: already in the journal
            fields = {"src": place(root, rel), "detail": f"essai, rien n'a bougé : {self.why(e, root)}", **({**ai, "confidence": conf} if ai else {})}
            if decision == "archiver":
                self.later.append(("à archiver", os.path.basename(p), {**fields, "dst": place(root, nfc(os.path.join(archive, rel)))}))   # the name stays
            else:
                self.later.append(("à supprimer", os.path.basename(p), {**fields, "dst": "corbeille du Drive" if root == "drive" else "Corbeille du Mac"}))
        if verdicts:
            words = ("à archiver", "à supprimer") if trial else ("archivé(s)", "supprimé(s)")
            summary = (f"{len(verdicts)} document(s) relu(s) : {counts['garder']} gardé(s), {counts['archiver']} {words[0]}, {counts['supprimer']} {words[1]}"
                       + (f", {len(unanswered)} sans réponse" if unanswered else "")
                       + (" ; essai : rien n'a bougé" if trial else ""))
            self.later.insert(start, ("revu", place(root, rel), {"detail": summary}))   # before its decisions: above them in the journal, newest first
            log(f"REVU    {place(root, rel)} : {summary}")
            sweep = self.state.setdefault("sweep", {})
            for k, n in (("files", len(verdicts)), ("archiver", counts["archiver"]), ("supprimer", counts["supprimer"])):
                sweep[k] = sweep.get(k, 0) + n
        if out_of_reach:   # a model failed or could not be reached: the review waits an hour, the inboxes do not
            self.state["pause_until"] = self.now + REVIEW_PAUSE
            why = public_reason(failure, "decide") if failure else "modèle local injoignable"
            REPORT.set("decide", "failed", f"relecture des dossiers en pause une heure : {why}")
            log(f"review: {len(unanswered)} document(s) without an answer ({why}): the review waits an hour")
        else:
            REPORT.set("decide", "ok")
        # the folder is done once its documents are read; after a failure, the same ones are asked again, three times at most
        if complete and not (out_of_reach and any(self.files[keys[p]]["tries"] < len(REVIEW_RETRIES) for p in retried)):
            self.state["cursor"] = list(here)
        return True

    def walk_over(self):
        """Every folder read: the next walk in REVIEW_REST; what no longer exists is forgotten; the end of the first walk is notified."""
        sweep = self.state.pop("sweep", None) or {}
        self.state.update(cursor=None, rest_until=self.now + REVIEW_REST)
        for k in [k for k, e in self.files.items() if self.mounted(k.split("|", 1)[0])
                  and not os.path.exists(os.path.join(DRIVE if k.startswith("drive|") else ICLOUD, e.get("path") or "?"))]:
            del self.files[k]
        if sweep.get("files"):
            log(f"REVU    every theme folder read: {sweep['files']} document(s), {sweep.get('archiver', 0)} to archive, {sweep.get('supprimer', 0)} to trash")
        if sweep.get("files") and not self.state.get("first_sweep"):
            self.state["first_sweep"] = self.now
            if CLEANUP == "on":
                n, a, s = sweep["files"], sweep.get("archiver", 0), sweep.get("supprimer", 0)
                notify(f"Relecture des dossiers terminée : {n} documents relus, {a} archivés, {s} mis à la corbeille")
            else:   # the trial's proposals as they stand: a document read twice in the walk (asked again under new rules) counts once
                decided = [e for e in self.files.values() if e.get("decision")]
                a, s = (sum(e["decision"] == d and e.get("state") == "pending" for e in decided) for d in ("archiver", "supprimer"))
                notify(f"Relecture d'essai des dossiers terminée : {len(decided)} documents relus, {a} à archiver et {s} à supprimer proposés, "
                       "rien n'a bougé. Le détail est dans le journal d'activité")

    def bilan(self):
        """Once per REVIEW_BILAN, what review.json holds, in the journal: every document of the theme folders, folder by folder, with what
        becomes of it. BILAN lines « i/n » of JSON, [{"r": root, "f": folder, "d": [documents]}], REVIEW_BILAN_LINE characters at most (a big
        folder goes on in the next line); a document is [name, code, …]: « g » kept (its certainty, and when the model wanted it out without
        enough certainty, what it wanted and why), « a » into the archive and « s » to the trash (certainty, reason as the journal says it),
        « x » a move that failed, « f » a format the review does not read, « u » left without an answer, « n » not read yet. Then one line
        of totals. Nothing is asked to a model; a root whose mount is down waits for a later pass."""
        if self.state.get("bilan") == REVIEW_BILAN:
            return
        roots = [r for r in ("icloud", "drive") if self.mounted(r)]
        if len(roots) < 2:
            return   # a mount down (iCloud's monthly reconnection): every document would look gone
        out, line, used, totals, count = [], [], 0, {}, 0
        for here, root, base, r, rel, names in theme_walk(None):
            piece = None
            for f in sorted(names, key=lambda n: nfc(n).lower()):
                p = os.path.join(r, f)
                if f.startswith(".") or not f.lower().endswith(REVIEW_EXT):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                e = self.files.get(f"{root}|{nfc(os.path.relpath(p, base))}|{st.st_size}") or {}
                state, decision, conf = e.get("state"), e.get("decision"), e.get("confidence")
                if state == "done":
                    continue   # moved already: the mount still shows it a few minutes
                if not e:
                    d = [nfc(f), "n"]
                elif state == "unanswered":
                    d = [nfc(f), "u"]
                elif state in ("pending", "failed") and decision in REVIEW_KINDS:
                    if root in REVIEW_NO_TRASH and decision == "supprimer":
                        e = {**e, "decision": "archiver", "proposal": "supprimer"}
                    d = [nfc(f), "x" if state == "failed" else "a" if e["decision"] == "archiver" else "s", conf, self.why(e, root)]
                elif e.get("reason") == "format que la relecture ne lit pas" and not e.get("engine"):
                    d = [nfc(f), "f"]
                else:
                    wanted = e.get("proposal")
                    d = [nfc(f), "g", conf] + ([wanted, e.get("reason") or ""] if wanted in REVIEW_KINDS else [])
                totals[d[1]] = totals.get(d[1], 0) + 1
                count += 1
                size = len(json.dumps(d, ensure_ascii=False)) + 1
                if piece is None or used + size > REVIEW_BILAN_LINE:
                    head = len(json.dumps({"r": root, "f": rel, "d": []}, ensure_ascii=False)) + 1
                    if line and used + head + size > REVIEW_BILAN_LINE:
                        out.append(line)
                        line, used = [], 0
                    piece = {"r": root, "f": rel, "d": []}
                    line.append(piece)
                    used += head
                piece["d"].append(d)
                used += size
        if line:
            out.append(line)
        for i, part in enumerate(out, 1):
            log(f"BILAN   {i}/{len(out)} {json.dumps(part, ensure_ascii=False, separators=(',', ':'))}")
        words = {"g": "kept", "a": "to archive", "s": "to trash", "x": "failed", "f": "unreadable", "u": "without an answer", "n": "not read yet"}
        log(f"BILAN   end: {count} document(s), " + ", ".join(f"{totals[k]} {w}" for k, w in words.items() if totals.get(k))
            + f" (FILE_SORTER_CLEANUP={CLEANUP})")
        self.state["bilan"] = REVIEW_BILAN


def stopped(signum, frame):
    """systemd stops the pass (time is over, or the server shuts down): the step it was at fails, then the pass ends."""
    REPORT.fail("arrêté avant la fin : délai dépassé ou arrêt du serveur")
    activity.failed("passage du tri", "passage arrêté avant la fin : délai dépassé ou arrêt du serveur")
    sys.exit(128 + signum)


def decide():
    """Server: decides for the inboxes (the Drive's 00 À trier and 00 Factures entrantes, Chrome's folder and the Drive's root, iCloud's
    00 À trier, the Drive's 00 Pièces jointes). Into iCloud, and out of iCloud, only the Mac moves: plan.json keeps the actions it has not
    returned yet. Inside the Drive, with FILE_SORTER_DRIVE_RCLONE, the server files itself: editable documents into the tree, doubtful
    invoices and downloads into 00 À trier,
    duplicates to the Drive's trash. A source already planned or just handled is not asked about again.
    Then the review of the theme folders (FILE_SORTER_CLEANUP trial or on, class Review): what the Mac did with its actions, the decisions
    waiting once the trial is lifted, and, when the inboxes left the models free, the next documents of a folder.
    The journal gets each decision and each action (the server's at once, the plan's once plan.json is written, so that a pass that
    fails before is not logged twice) and what the Mac returned; the step report follows the pass."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    signal.signal(signal.SIGTERM, stopped)
    failures, in_drive, later, failure = [], [], [], None   # later: (action, object, fields) of the plan's events; failure: Claude's
    try:
        REPORT.running("inboxes")
        plan = read_json(PLAN, {"actions": [], "faites": {}})
        results = read_json(RESULTS, {})
        returned = results.get("appliques", {})
        done = {k: v for k, v in plan.get("faites", {}).items() if time.time() - v[1] < 3600}   # sources handled, until the mounts show it
        actions, came_back, reviewed = [], [], []   # reviewed: the review's actions the Mac returned, with their status
        try:
            review = Review(actions, later)
        except Exception as e:   # never the inboxes held back by the review
            review = None
            log(f"ERREUR  review: {type(e).__name__} {e}")
        withdrawn = review.withdrawn if review else {}   # the review's actions taken out of the plan, while the Mac may still return them

        def came(a):   # an action the Mac returned: its event, and the review's state
            done[f"{a['source']['racine']}|{a['source']['chemin']}"] = [returned[a["id"]], time.time()]
            log(f"MAC     {a['type']} {a['source']['chemin']} : {returned[a['id']]}")
            came_back.append(returned[a["id"]])
            later.append(mac_event(a, returned[a["id"]]))
            if a.get("type") in REVIEW_KINDS:
                reviewed.append((a, returned[a["id"]]))
            withdrawn.pop(a["id"], None)

        for a in plan.get("actions", []):
            if a["id"] in returned:
                came(a)
            elif a.get("type") in REVIEW_KINDS and CLEANUP != "on":   # the review stopped or back to its trial: what the Mac has not done yet
                withdrawn[a["id"]] = {"action": a, "time": time.time()}   # leaves the plan; its decision waits in review.json
                later.append(("annulé", os.path.basename(a["source"]["chemin"]), {"src": place(a["source"]["racine"], os.path.dirname(a["source"]["chemin"])),
                                                                                  "detail": f"relecture {'arrêtée' if CLEANUP == 'off' else 'revenue à son essai'} : "
                                                                                            "retiré du plan, le Mac ne le fera pas"}))
                log(f"review: {a['type']} {a['source']['chemin']} withdrawn from the Mac's plan (FILE_SORTER_CLEANUP={CLEANUP})")
            elif time.time() - a.get("cree_le", 0) < 7 * 86400:   # an action not returned within a week is dropped
                actions.append(a)
                withdrawn.pop(a["id"], None)   # planned again, the review back on
        for ident, w in list(withdrawn.items()):   # withdrawn, but the Mac had fetched the plan before and did it anyway
            if ident in returned and ident not in {a["id"] for a in plan.get("actions", [])}:
                came(w["action"])
            elif time.time() - w.get("time", 0) > 8 * 86400:
                del withdrawn[ident]   # the Mac keeps what it applied a week: nothing can come back after
        if came_back:
            errors = sum(status not in MAC_FINE for status in came_back)
            REPORT.set("icloud", *(("failed", f"le Mac n'a pas pu appliquer {errors} action(s) du plan") if errors else ("ok",)))
        elif not actions:
            REPORT.set("icloud", "skipped")   # while the Mac has actions to apply, its step stays without a state
        planned = {f"{a['source']['racine']}|{a['source']['chemin']}" for a in actions} | set(done)
        files = pending((GMAIL_INBOX, DRIVE_INBOX) + CHROME_INBOXES + (DRIVE, INBOX, ATTACH_INBOX), skip=lambda p: "|".join(relative(p)) in planned)
        server_side = lambda p: bool(DRIVE_RCLONE) and os.path.dirname(p) in DRIVE_INBOXES

        def by_server(p, what, act):   # a move in the Drive done here; a failure is kept for the end of the pass, the file stays
            REPORT.running("drive")
            try:
                result = act()
                done["|".join(relative(p))] = [f"ok (serveur) : {result}", time.time()]
                in_drive.append(p)
                return result
            except DriveError as e:
                failures.append(os.path.basename(p))
                log(f"ERREUR  {what} {os.path.basename(p)} : {e}")
                activity.log("échec", os.path.basename(p), src=where(os.path.dirname(p)),
                             detail=f"{DRIVE_WHAT.get(what, what)} impossible, nouvel essai au prochain passage : {e}")
                return None

        found, duplicates = bool(files), []
        if files:
            files, duplicates = dedupe(files)
        REPORT.set("inboxes", *(("failed", "arrivée illisible pour l'instant : " + ", ".join(UNREADABLE)) if UNREADABLE
                                else ("ok",) if found else ("skipped",)))
        for p, orig in duplicates:
            name = os.path.basename(p)
            if server_side(p):
                if by_server(p, "doublon", lambda: rclone("deletefile", "--drive-use-trash", DRIVE_RCLONE + os.path.relpath(p, DRIVE)) or "corbeille du Drive"):
                    log(f"DOUBLON {name}  (identical to {orig}) -> the Drive's trash, by the server")
                    activity.log("doublon", name, src=where(os.path.dirname(p)), dst="corbeille du Drive",
                                 detail=f"identique à {twin(orig)} : mis à la corbeille du Drive par le serveur")
            else:
                actions.append(make_action("doublon", p, original=nfc(orig)))
                log(f"DOUBLON {name}  (identical to {orig}) -> Trash, by the Mac")
                later.append(("doublon", name, {"src": where(os.path.dirname(p)), "dst": "Corbeille du Mac",
                                                "detail": f"identique à {twin(orig)} : le Mac le met à la Corbeille à son prochain passage"}))
        if not files:
            REPORT.set("decide", "skipped")
        else:
            REPORT.running("decide")
            choices, failure, waiting = choose(files)
            REPORT.set("decide", *(("failed", f"le modèle local n'a pas répondu pour {waiting} fichier(s) : nouvel essai au prochain passage")
                                   if waiting else ("ok",)))
            for src, dest, new, conf, d, model in choices:
                name, origin = os.path.basename(src), where(os.path.dirname(src))
                ai = {**model, "confidence": conf}
                if dest is None:
                    signature = sig(src)   # before a move: the mount may stop showing the file at once
                    why = kept_why(conf, d)
                    if os.path.dirname(src) in (GMAIL_INBOX,) + CHROME_INBOXES + (DRIVE,):   # a doubtful invoice or download joins the inbox the owner looks at; an attachment stays
                        if server_side(src):
                            moved = by_server(src, "vers 00 À trier", lambda: drive_move(src, os.path.relpath(DRIVE_INBOX, DRIVE), name))
                            if not moved:
                                continue
                            activity.log("à trier", name, src=origin, dst=place("drive", moved), detail=f"{why} ; mis dans 00 À trier par le serveur", **ai)
                        else:
                            actions.append(make_action("vers_a_trier", src))
                            later.append(("à trier", name, {"src": origin, "dst": where(DRIVE_INBOX),
                                                            "detail": f"{why} ; le Mac le met dans 00 À trier à son prochain passage", **ai}))
                    else:
                        activity.log("gardé", name, src=origin, detail=why, **ai)
                    save_kept(src, signature)
                    log(f"GARDÉ   {name}  (confidence {conf:.2f}, proposed: {d.get('destination')}) — {d.get('raison')}")
                    continue
                base = "drive" if name.lower().endswith(OFFICE) else "icloud"
                if base == "drive" and server_side(src):
                    target = by_server(src, "rangement", lambda: drive_move(src, dest, new))
                    if target:
                        log(f"RANGÉ   {name}  ->  Drive/{target}  ({conf:.2f}) — {d.get('raison')} (by the server)")
                        for action, obj, fields in filed(name, origin, place("drive", target), d, ai, "rangé par le serveur dans le Drive"):
                            activity.log(action, obj, **fields)
                    continue
                actions.append(make_action("ranger", src, cible={"racine": base, "chemin": nfc(os.path.join(dest, new))}, confiance=conf, raison=d.get("raison", "")))
                log(f"PLAN    {name}  ->  {'Drive/' if base == 'drive' else ''}{os.path.join(dest, new)}  ({conf:.2f}) — {d.get('raison')}")
                later += filed(name, origin, place(base, nfc(os.path.join(dest, new))), d, ai, "le Mac le range à son prochain passage")
        if review:
            try:
                review.planned, review.by_server = {f"{a['source']['racine']}|{a['source']['chemin']}" for a in actions}, by_server
                for a, status in reviewed:
                    review.returned(a, status)
                if CLEANUP == "on":
                    review.apply_pending()
                if CLEANUP in ("trial", "on") and not files:   # the models are free: the inboxes had nothing for them in this pass
                    review.step()
                if CLEANUP in ("trial", "on"):
                    try:
                        review.bilan()
                    except Exception as e:   # a bilan that fails holds nothing back, and is not tried again before a new REVIEW_BILAN
                        review.state["bilan"] = REVIEW_BILAN
                        log(f"ERREUR  review: bilan {type(e).__name__} {e}")
            except Exception as e:   # a review that fails never holds back the inboxes: it waits an hour
                review.state["pause_until"] = time.time() + REVIEW_PAUSE
                log(f"ERREUR  review: {type(e).__name__} {e}")
                REPORT.set("decide", "failed", f"relecture des dossiers en pause une heure : erreur {type(e).__name__}")
                activity.log("échec", "relecture des dossiers", detail=f"en pause une heure : {type(e).__name__} : {e}")
        REPORT.set("drive", *(("failed", f"{len(failures)} déplacement(s) dans le Drive en échec, nouvel essai au prochain passage") if failures
                              else ("ok",) if in_drive else ("skipped",)))
        REPORT.running("plan")
        write_json(PLAN, {"genere_a": datetime.now().isoformat(timespec="seconds"), "actions": actions, "faites": done})
        if review and review.changed():
            try:
                review.save()
            except OSError as e:   # the next pass reads the former state again: at worst a document is asked about twice
                log(f"ERREUR  review.json not written: {e}")
        REPORT.set("plan", "ok" if actions else "skipped")
        for action, obj, fields in later + downloads_events(results.get("telechargements") or []):
            activity.log(action, obj, **fields)
        if failure:   # Claude failed: the files it was asked about wait; the others are done and the plan is written
            REPORT.running("decide")
            raise failure
        activity.recovered("passage du tri")
    except Exception as e:
        log(f"ERREUR  {e}")
        reason = public_reason(e, REPORT.current())
        REPORT.fail(reason)
        activity.failed("passage du tri", f"{reason} ; {type(e).__name__} : {e}")
        notify("Erreur du tri des fichiers sur le serveur : voir son journal")
        raise
    finally:
        lock.close()
    REPORT.finish()
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
        write_json(RESULTS, {"rendu_le": datetime.now().isoformat(timespec="seconds"), "appliques": {k: v[0] for k, v in applied.items()},
                             "telechargements": read_json(DOWNLOADS_SEEN, [])})
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
    """One action of the plan, after checks: paths inside iCloud or the Drive, source in an inbox (in a theme folder for the review's
    archiver and supprimer: never an inbox nor the archive; archiver moves only into the archive of the same root), size unchanged.
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
    if a["type"] in REVIEW_KINDS:
        top = nfc(a["source"]["chemin"]).split("/")
        if len(top) < 2 or not THEME.match(top[0]) or top[0].startswith("00 ") or is_archive(top[0]):
            raise ValueError("source hors des dossiers par thèmes")
        if any(b in "/" + "/".join(top[:-1]) for b in tuple(SETTINGS.get("paquets") or ()) + (PHOTOS,)):
            raise ValueError("source dans un dossier que la relecture ne touche pas")
    elif nfc(os.path.dirname(src)) not in tuple(nfc(x) for x in (INBOX, GMAIL_INBOX, DRIVE_INBOX, ATTACH_INBOX) + CHROME_INBOXES + (DRIVE,)):
        raise ValueError("source hors des boîtes d'arrivée")
    if not os.path.exists(src):
        return "absent"
    if a["type"] not in REVIEW_KINDS and not materialize(src):   # a document only in the cloud is moved or trashed as it is, not downloaded first
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
    elif a["type"] == "archiver":   # the review: a document no longer current, into the archive, at the same path
        if a["cible"]["racine"] != a["source"]["racine"] or not is_archive(nfc(a["cible"]["chemin"]).split("/")[0]):
            raise ValueError("destination hors des archives")
        target = unique(resolve(a["cible"]))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(src, target)
        prune(os.path.dirname(src), roots[a["source"]["racine"]])
        log(f"ARCHIVÉ {name}  ->  {'Drive/' if a['cible']['racine'] == 'drive' else ''}{os.path.relpath(target, roots[a['cible']['racine']])} — {a.get('raison', '')}")
    elif a["type"] == "supprimer":   # the review: a document of no use, or a lesser copy, to the Trash
        if a["source"]["racine"] in REVIEW_NO_TRASH:   # the owner's rule of 06/11/2026: the server sends no such action
            raise ValueError("rien n'est mis à la corbeille dans le Drive")
        kept = a.get("garde")   # a copy: the one kept in its place, still in its folder or archived since
        if kept and not any(os.path.exists(os.path.join(roots[kept["racine"]], *x, nfc(kept["chemin"])))
                            for x in ((), (archive_of(roots[kept["racine"]]),))):
            raise ValueError("l'exemplaire gardé à sa place n'est plus là")
        to_trash(src)
        prune(os.path.dirname(src), roots[a["source"]["racine"]])
        log(f"SUPPRIMÉ {name}  -> Trash — {a.get('raison', '')}")
    else:
        raise ValueError(f"action inconnue : {a['type']}")
    return "ok"


def prune(folder, base):
    """Mac: the folders an archiving or a deletion left empty go (a year, a quarter, a former case), but never a theme folder nor its
    first sub-folders (« 20 Administratif/Logement »): they are the tree the sorting files into."""
    while len(nfc(os.path.relpath(folder, base)).split(os.sep)) > 2 and not os.path.relpath(folder, base).startswith(".."):
        try:
            if [f for f in os.listdir(folder) if f != ".DS_Store"]:
                return
            if os.path.exists(os.path.join(folder, ".DS_Store")):
                os.remove(os.path.join(folder, ".DS_Store"))
            os.rmdir(folder)
        except OSError:
            return
        log(f"VIDÉ    {os.path.relpath(folder, base)} (empty folder removed)")
        folder = os.path.dirname(folder)


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


DOWNLOADS_SEEN = os.path.join(DATA, "downloads.json")   # Mac: what move_downloads did in the last 7 days, sent back in results.json


def note_download(action, name, size, dest, detail=""):
    """Mac: remembers a download handled, for the server's activity journal (the Mac writes no journal itself)."""
    seen = [x for x in read_json(DOWNLOADS_SEEN, []) if time.time() - x.get("t", 0) < 7 * 86400]
    when = datetime.now().isoformat(timespec="seconds")
    seen.append({"id": hashlib.sha1(f"{name}|{size}|{when}".encode()).hexdigest()[:16], "t": time.time(), "quand": when,
                 "action": action, "nom": name, "taille": size, "vers": dest, "detail": detail})
    write_json(DOWNLOADS_SEEN, seen[-300:])


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
            note_download("laissé", f, st.st_size, "Téléchargements", "pas un document : installeur, archive, vidéo… gardé dans Téléchargements")
            continue
        if st.st_size > MAX_SIZE:
            save_kept(p)
            log(f"TROP GROS {f}  ({st.st_size // 2**20} MB) left in Downloads")
            notify(f"{f} ({st.st_size // 2**20} Mo) trop gros pour le Drive, laissé dans Téléchargements")
            note_download("trop gros", f, st.st_size, "Téléchargements", f"{st.st_size // 2**20} Mo, au-delà de {MAX_SIZE // 2**20} Mo : gardé dans Téléchargements")
            continue
        os.makedirs(DRIVE_INBOX, exist_ok=True)
        target = unique(os.path.join(DRIVE_INBOX, f))
        shutil.move(p, target)
        log(f"VERSÉ   {f}  -> Drive/00 À trier")
        note_download("versé", f, st.st_size, where(target), "téléchargement du Mac versé dans la boîte du Drive, que le serveur trie")


if __name__ == "__main__":
    # an unknown or former option stops here: falling back to the full mode would decide and move files
    args = sys.argv[1:]
    if "--deposit" in args:
        args = args[:args.index("--deposit") + 1] + args[args.index("--deposit") + 4:]
    unknown = [a for a in args if a not in ("--dry-run", "--decide", "--apply", "--deposit")]
    if unknown:
        sys.exit(f"unknown argument {unknown[0]}: sort.py [--dry-run] | --decide | --apply | --deposit <icloud|drive> <path> <file>")
    {"decide": decide, "apply": apply_plan, "deposit": deposit}.get(ROLE, main)()
