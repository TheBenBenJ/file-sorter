#!/usr/bin/env python3
"""Trie automatiquement iCloud Drive/00 À trier et ~/Downloads dans l'arborescence numérotée.

Claude (CLI, outil Read seulement) propose une destination par fichier ;
ce script valide et déplace. Jamais d'écrasement, doute => le fichier reste.
"""
import fcntl, json, os, re, shutil, subprocess, sys, tempfile, time, unicodedata
from datetime import datetime

HOME = os.path.expanduser("~")
IC = os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
INBOX = os.path.join(IC, "00 À trier")
DOWNLOADS = os.path.join(HOME, "Downloads")
ICI = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ICI, "reglages.json"), encoding="utf-8") as f:
    REGLAGES = json.load(f)   # identité et chemins personnels, hors du dépôt (voir reglages.exemple.json)
DRIVE = os.path.join(HOME, REGLAGES["drive"])
GMAIL_INBOX = os.path.join(DRIVE, "00 Factures entrantes")  # rempli par gmail-factures.gs
EXTERNES = (DOWNLOADS, GMAIL_INBOX)
BUREAUTIQUE = (".docx", ".doc", ".odt", ".xlsx", ".xls", ".ods")   # éditables : vont dans le Drive, même arborescence, convertis en Google Docs/Sheets par le script   # sources hors iCloud : un fichier douteux part dans 00 À trier
PREVIEWS = tempfile.mkdtemp(prefix="tri-icloud-")
PARTIAL_EXT = (".crdownload", ".download", ".part", ".partial", ".tmp", ".opdownload")
MAX_SIZE = 500 * 1024 * 1024   # au-delà, le fichier reste dans Téléchargements (dumps...)
LOG = os.path.join(HOME, "Library/Logs/tri-icloud.log")
LOCK = os.path.join(ICI, "tri.lock")
GARDES = os.path.join(ICI, "gardes.json")   # fichiers laissés, à ne pas redemander
CLAUDE = "/opt/homebrew/bin/claude"
MODEL = os.environ.get("TRI_MODEL", "haiku")
MIN_AGE = 60          # secondes sans modification avant de traiter un fichier
MAX_FILES = 15        # par passage
SEUIL = 0.7           # confiance minimale pour déplacer
DRY = "--dry-run" in sys.argv

TEXT_EXT = {".txt", ".csv", ".ics", ".md", ".json", ".html", ".htm", ".xml"}
TEXTUTIL_EXT = {".doc", ".docx", ".rtf", ".odt", ".pages"}
VISION_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line)


def notify(msg):
    subprocess.run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "Tri iCloud"',
                    "-e", "end run", msg], check=False)


def folders():
    out = []
    for r, ds, _ in os.walk(IC):
        ds[:] = sorted(d for d in ds if not d.startswith("."))
        rel = os.path.relpath(r, IC)
        if rel == "." or rel.startswith("00 "):
            continue
        if rel.count(os.sep) < 4:
            out.append(rel)
    return out


def materialize(p, attente=120):
    """Télécharge le fichier iCloud s'il n'est que dans le cloud. False si indisponible."""
    for i in range(attente // 3):
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
    st = os.stat(p)
    return f"{os.path.basename(p)}|{st.st_size}|{int(st.st_mtime)}"


def load_gardes():
    try:
        return set(json.load(open(GARDES)))
    except Exception:
        return set()


def save_garde(p):
    g = load_gardes()
    g.add(sig(p))
    json.dump(sorted(g), open(GARDES, "w"), ensure_ascii=False, indent=0)


TROP_GROS = set()


def pending():
    now = time.time()
    gardes = load_gardes()
    files = []
    for src in (DOWNLOADS, GMAIL_INBOX, INBOX):
        try:
            entrees = sorted(os.listdir(src)) if os.path.isdir(src) else []
        except OSError as e:   # dossier Drive/iCloud en cours de synchro (EDEADLK) : on réessaiera au prochain passage, sans alerte
            log(f"ATTENTE source illisible pour l'instant : {os.path.basename(src)} ({e.strerror})")
            continue
        for f in entrees:
            p = os.path.join(src, f)
            if f.startswith(".") or f.lower().endswith(PARTIAL_EXT) or not os.path.isfile(p):
                continue
            st = os.stat(p)
            if now - max(st.st_mtime, st.st_ctime) < MIN_AGE or sig(p) in gardes:
                continue
            if src == DOWNLOADS and st.st_size > MAX_SIZE:
                if p not in TROP_GROS:
                    TROP_GROS.add(p)
                    log(f"TROP GROS {f}  ({st.st_size // 2**20} Mo) laissé dans Téléchargements")
                    notify(f"{f} ({st.st_size // 2**20} Mo) trop gros pour iCloud, laissé dans Téléchargements")
                continue
            files.append(p)
    return files[:MAX_FILES]


def extract(p):
    """Texte court du fichier, ou None s'il faut le lire visuellement."""
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == ".pdf":
            t = subprocess.run(["pdftotext", "-l", "3", "-enc", "UTF-8", p, "-"],
                               capture_output=True, text=True, timeout=60).stdout
        elif ext in TEXTUTIL_EXT:
            t = subprocess.run(["textutil", "-convert", "txt", "-stdout", p],
                               capture_output=True, text=True, timeout=60).stdout
        elif ext in (".pptx", ".xlsx", ".docm", ".xlsm"):
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
REGLES = open(os.path.join(ICI, "regles.md"), encoding="utf-8").read().strip()


def ask_claude(items, tree):
    blocs = []
    for p, txt in items:
        name = os.path.basename(p)
        if txt:
            blocs.append(f"### {name}\nExtrait du texte :\n{txt}")
        elif os.path.splitext(p)[1].lower() in (".heic", ".heif", ".tiff"):
            jpg = os.path.join(PREVIEWS, re.sub(r"\W", "_", name) + ".jpg")
            subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", p, "--out", jpg], capture_output=True)
            blocs.append(f"### {name}\nImage : lis l'aperçu JPEG avec l'outil Read à ce chemin exact : {jpg}")
        else:
            blocs.append(f"### {name}\nPas de texte extractible : lis-le avec l'outil Read à ce chemin exact : {p}")
    prompt = f"""Tu ranges les documents personnels de {REGLAGES['proprietaire']} dans son iCloud Drive.

Dossiers existants (chemins relatifs, à réutiliser tels quels) :
{chr(10).join('- ' + f for f in tree)}

Règles :
- "destination" = un dossier de la liste ci-dessus. Tu peux ajouter UN niveau de sous-dossier sous un dossier existant si c'est clairement utile (ex. "50 Travail/Auto-entrepreneur/Factures 2026/T4" pour une facture d'octobre 2026 ; trimestres T1=jan-mars, T2=avr-juin, T3=juil-sept, T4=oct-déc).
{REGLES}
- "nouveau_nom" : TOUJOURS fourni, conforme à la convention ci-dessous (null uniquement si le nom actuel la respecte déjà exactement).

{CONVENTION}
- "confiance" entre 0 et 1. Si tu n'es pas sûr, mets moins de 0.7 : le fichier restera dans "00 À trier".
- "raison" : une phrase courte.
- Le champ "fichier" doit reprendre EXACTEMENT le nom donné après "###". Réponds pour CHAQUE fichier.

Fichiers à classer :

{chr(10).join(blocs)}
"""
    cmd = [CLAUDE, "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--tools", "Read", "--allowedTools", "Read",
           "--add-dir", INBOX, "--add-dir", DOWNLOADS, "--add-dir", GMAIL_INBOX, "--add-dir", PREVIEWS, "--strict-mcp-config", "--setting-sources", "project",
           "--no-session-persistence"]
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir(), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude a échoué ({r.returncode}) : {r.stderr[-500:] or r.stdout[-500:]}")
    data = json.loads(r.stdout)
    res = data.get("structured_output") or data.get("result")
    if isinstance(res, str):
        res = json.loads(re.search(r"\{.*\}", res, re.S).group(0))
    return res["fichiers"]


def safe_dest(rel, tree):
    rel = os.path.normpath(rel.strip().strip("/"))
    if rel.startswith("..") or os.path.isabs(rel) or rel.startswith("00 ") or rel == ".":
        return None
    if rel in tree:
        return rel
    parent = os.path.dirname(rel)
    return rel if parent in tree else None   # un seul niveau créé au maximum


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
    import hashlib
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def to_trash(p):
    subprocess.run(["trash", p], capture_output=True)
    if os.path.exists(p):   # trash refuse parfois les fichiers iCloud
        shutil.move(p, unique(os.path.join(HOME, ".Trash", os.path.basename(p))))


def dedupe(files):
    """Envoie à la Corbeille les fichiers déjà présents à l'identique dans iCloud (ou dans la file)."""
    sizes = {os.path.getsize(p) for p in files}
    known = {}
    for r, ds, fs in os.walk(IC):
        ds[:] = [d for d in ds if not d.startswith(".") and not d.startswith("00 ")]
        for f in fs:
            q = os.path.join(r, f)
            if not f.startswith(".") and os.path.getsize(q) in sizes and materialize(q, 30):
                try:
                    known.setdefault(sha(q), q)
                except OSError:
                    pass
    keep = []
    for p in sorted(files, key=lambda x: (bool(re.search(r"\(\d+\)", x)), len(x))):
        h = sha(p)
        if h in known:
            if not DRY:
                to_trash(p)
            log(f"DOUBLON {os.path.basename(p)}  (identique à {os.path.relpath(known[h], IC)}) -> Corbeille")
        else:
            known[h] = p
            keep.append(p)
    return keep


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # libéré automatiquement si le processus meurt
    except OSError:
        return
    try:
        moved, kept = [], []
        done = set()
        while True:
            files = [p for p in pending() if p not in done]
            if not files:
                break
            done.update(files)
            ready = []
            for p in files:
                if materialize(p):
                    ready.append(p)
                else:
                    log(f"ATTENTE {os.path.basename(p)}  (pas encore téléchargé depuis iCloud, réessai au prochain passage)")
            files = dedupe(ready)
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
    # état du rangement pour le Dashboard (sans IA, noms et dates seulement)
    subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit-rangement.py")], capture_output=True, timeout=300)


def batch(files, moved, kept):
        tree = folders()
        items = [(p, extract(p)) for p in files]
        decisions = ask_claude(items, tree)
        nfc = lambda x: re.sub(r"[^0-9a-z]", "", unicodedata.normalize("NFKD", x).lower())
        by_name = {nfc(os.path.basename(p)): p for p in files}
        answered = set()
        for d in decisions:
            src = by_name.get(nfc(d.get("fichier", "")))
            if not src or not os.path.exists(src):
                continue
            answered.add(src)
            name = os.path.basename(src)
            dest = safe_dest(d.get("destination", ""), tree)
            conf = float(d.get("confiance", 0))
            if not dest or conf < SEUIL:
                kept.append(name)
                if not DRY and os.path.dirname(src) in EXTERNES:
                    target = unique(os.path.join(INBOX, name))
                    shutil.move(src, target)
                    save_garde(target)
                elif not DRY:
                    save_garde(src)
                log(f"GARDÉ   {name}  (confiance {conf:.2f}, proposé : {d.get('destination')}) — {d.get('raison')}")
                continue
            new = name
            if d.get("nouveau_nom") and not dest.startswith("80 Images/Photos") and not dest.startswith("80 Images/Captures"):
                new = clean_name(d["nouveau_nom"], name)
            base = DRIVE if name.lower().endswith(BUREAUTIQUE) else IC
            target = unique(os.path.join(base, dest, new))
            if DRY:
                log(f"[essai] {name}  ->  {os.path.relpath(target, IC)}  ({conf:.2f}) — {d.get('raison')}")
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(src, target)
            moved.append(f"{new} → {dest}")
            log(f"RANGÉ   {name}  ->  {'Drive/' if base == DRIVE else ''}{os.path.relpath(target, base)}  ({conf:.2f}) — {d.get('raison')}")
        for p in files:
            if p not in answered:
                log(f"SANS RÉPONSE {os.path.basename(p)}  (réessai au prochain passage)")


if __name__ == "__main__":
    main()
