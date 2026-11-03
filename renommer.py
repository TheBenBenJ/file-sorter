#!/usr/bin/env python3
"""Renames existing iCloud documents according to convention.md.

  renommer.py plan       -> proposes names (Claude), writes plan-renommage.json + .tsv, touches nothing
  renommer.py appliquer  -> applies the plan, undo journal in annulation-renommage.json
  renommer.py annuler    -> restores the old names
Environment: RACINE (another folder than iCloud, e.g. the Drive), SUFFIXE (separate plan and undo files, e.g. "-drive"),
EXCLUS (folders to skip, separated by "|"), DOCS_SEULEMENT (rename documents only).
"""
import concurrent.futures, hashlib, json, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tri  # reuses the extraction, the Claude settings and the convention

HERE = os.path.dirname(os.path.abspath(__file__))
SUFFIX = os.environ.get("SUFFIXE", "")            # e.g. "-drive": separate plan and undo files
PLAN = os.path.join(HERE, "donnees", f"plan-renommage{SUFFIX}.json")
UNDO = os.path.join(HERE, "donnees", f"annulation-renommage{SUFFIX}.json")
ROOT = os.path.expanduser(os.environ.get("RACINE", tri.ICLOUD))   # iCloud by default, or another folder (Drive)
EXCLUDED = tuple(os.environ["EXCLUS"].split("|")) if os.environ.get("EXCLUS") else ("00 À trier", "80 Images")
DOCS = (".pdf", ".doc", ".docx", ".odt", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".heic", ".txt", ".rtf", ".mp4", ".mov", ".kmz")
DOCS_ONLY = bool(os.environ.get("DOCS_SEULEMENT"))   # Drive: rename documents only
CODE_MARKERS = {"package.json", "composer.json", "pom.xml", "build.gradle", ".git", "angular.json", "Makefile", ".idea", "src", "app", "www", "vendor", "node_modules"}
BUNDLES = ("_files", "kit-de-modeles", "retours-recette")   # folders whose files reference each other
BATCH = 20

# Answer format imposed on Claude; its French keys are part of the exchange with the model.
SCHEMA = {"type": "object", "properties": {"fichiers": {"type": "array", "items": {
    "type": "object",
    "properties": {"fichier": {"type": "string"}, "nouveau_nom": {"type": "string"}},
    "required": ["fichier", "nouveau_nom"]}}}, "required": ["fichiers"]}


def batches():
    for r, ds, fs in os.walk(ROOT):
        if CODE_MARKERS & set(ds + fs) or any(x.endswith((".sln", ".csproj", ".xcodeproj")) for x in ds + fs):
            ds[:] = []   # code project: left alone
            continue
        ds[:] = sorted(d for d in ds if not d.startswith(".") and not d.endswith(BUNDLES))
        rel = os.path.relpath(r, ROOT)
        if rel == "." or rel.startswith(EXCLUDED):
            continue
        fs = sorted(f for f in fs if not f.startswith(".") and (not DOCS_ONLY or f.lower().endswith(DOCS)))
        for i in range(0, len(fs), BATCH):
            yield rel, [os.path.join(r, f) for f in fs[i:i + BATCH]]


def propose(rel, files):
    # The prompt stays in French: the convention it applies produces French names.
    blocks = []
    for p in files:
        tri.materialize(p)
        txt = tri.extract(p)
        name = os.path.basename(p)
        blocks.append(f"### {name}\n" + (f"Extrait :\n{txt}" if txt else "(pas de texte : déduis du nom, ou lis-le avec Read si c'est un PDF ou une image : " + p + ")"))
    prompt = f"""Renomme les fichiers du dossier "{rel}" (documents de {tri.SETTINGS['proprietaire']}) selon cette convention :

{tri.CONVENTION}
Le dossier donne le thème : ne répète pas son nom dans le fichier sauf si c'est l'émetteur.
Si le nom respecte déjà la convention, renvoie-le à l'identique. N'invente pas de date : prends-la dans le contenu ou dans le nom actuel, sinon utilise la forme sans date.
Le champ "fichier" reprend EXACTEMENT le nom donné après "###". Réponds pour chaque fichier.

{chr(10).join(blocks)}
"""
    cmd = [tri.CLAUDE, "-p", prompt, "--model", tri.MODEL, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--tools", "Read", "--allowedTools", "Read",
           "--add-dir", ROOT, "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir(), env=env)
    data = json.loads(r.stdout)
    res = data.get("structured_output") or json.loads(data["result"])
    by = {tri.re.sub(r"[^0-9a-z]", "", tri.unicodedata.normalize("NFKD", os.path.basename(p)).lower()): p for p in files}
    out = []
    for d in res["fichiers"]:
        src = by.get(tri.re.sub(r"[^0-9a-z]", "", tri.unicodedata.normalize("NFKD", d["fichier"]).lower()))
        if src:
            new = tri.clean_name(d["nouveau_nom"], os.path.basename(src))
            out.append({"de": os.path.relpath(src, ROOT), "vers": os.path.join(rel, new)})
    return out


def sha(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def plan(prefix=None):
    todo = [b for b in batches() if not prefix or b[0].startswith(prefix)]
    print(f"{sum(len(f) for _, f in todo)} files in {len(todo)} batches", flush=True)
    res = []
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        for i, r in enumerate(ex.map(lambda a: propose(*a), todo)):
            res += r
            print(f"batch {i + 1}/{len(todo)}", flush=True)
    if prefix and os.path.exists(PLAN):   # replaces only the part planned again
        res = [e for e in json.load(open(PLAN)) if not e["de"].startswith(prefix)] + res
    for e in res:
        e.pop("collision_avec", None); e.pop("doublon_exact", None)
    # collisions and exact duplicates
    seen = {}
    for e in res:
        e["identique"] = e["de"] == e["vers"]
        k = e["vers"].lower()
        if k in seen:
            a, b = os.path.join(ROOT, seen[k]["de"]), os.path.join(ROOT, e["de"])
            e["collision_avec"] = seen[k]["de"]
            e["doublon_exact"] = sha(a) == sha(b)
        else:
            seen[k] = e
    json.dump(res, open(PLAN, "w"), ensure_ascii=False, indent=1)
    with open(PLAN.replace(".json", ".tsv"), "w") as f:
        for e in res:
            if not e["identique"]:
                flag = " [DOUBLON]" if e.get("doublon_exact") else (" [COLLISION]" if e.get("collision_avec") else "")
                f.write(f"{e['de']}\t{os.path.basename(e['vers'])}{flag}\n")
    n = sum(not e["identique"] for e in res)
    print(f"plan: {n} renames, {sum(1 for e in res if e.get('doublon_exact'))} duplicates, "
          f"{sum(1 for e in res if e.get('collision_avec') and not e.get('doublon_exact'))} collisions")


def apply():
    res = json.load(open(PLAN))
    undo_log = []
    # collisions: the most recent file is kept, the other one goes to the Trash
    losers = set()
    for e in res:
        if e.get("collision_avec") and not e.get("doublon_exact"):
            a, b = os.path.join(ROOT, e["de"]), os.path.join(ROOT, e["collision_avec"])
            if os.path.exists(a) and os.path.exists(b):
                losers.add(e["de"] if os.path.getmtime(a) < os.path.getmtime(b) else e["collision_avec"])
    for rel in losers:
        tri.to_trash(os.path.join(ROOT, rel))
        undo_log.append({"corbeille": rel})
    for e in res:
        if e["de"] in losers:
            continue
        if e["identique"]:
            continue
        src = os.path.join(ROOT, e["de"])
        if not os.path.exists(src):
            continue
        if e.get("doublon_exact"):
            tri.to_trash(src)
            undo_log.append({"corbeille": e["de"]})
            continue
        dst = os.path.join(ROOT, e["vers"])
        same = lambda x: tri.unicodedata.normalize("NFC", x).lower()
        if same(dst) != same(src):   # a mere change of case or accents: not a conflict
            dst = tri.unique(dst)
        os.rename(src, dst)
        undo_log.append({"de": e["de"], "vers": os.path.relpath(dst, ROOT)})
    json.dump(undo_log, open(UNDO, "w"), ensure_ascii=False, indent=1)
    print(f"{sum('vers' in u for u in undo_log)} renamed, {sum('corbeille' in u for u in undo_log)} duplicates sent to the Trash")


def undo():
    for u in reversed(json.load(open(UNDO))):
        if "vers" in u and os.path.exists(os.path.join(ROOT, u["vers"])):
            os.rename(os.path.join(ROOT, u["vers"]), os.path.join(ROOT, u["de"]))
    print("undone (the duplicates are in the Trash)")


if __name__ == "__main__":
    # the command words stay French: the rangement-cloud skill and the owner's habits use them
    {"plan": plan, "appliquer": apply, "annuler": undo}[sys.argv[1]](*sys.argv[2:])
