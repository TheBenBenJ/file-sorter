#!/usr/bin/env python3
"""Renomme les documents iCloud existants selon convention.md.

  renommer.py plan       -> propose (Claude) et écrit plan-renommage.json + .tsv, ne touche à rien
  renommer.py appliquer  -> applique le plan, journal d'annulation dans annulation-renommage.json
  renommer.py annuler    -> remet les anciens noms
"""
import concurrent.futures, hashlib, json, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tri  # réutilise extraction, Claude, convention

HERE = os.path.dirname(os.path.abspath(__file__))
SUFFIXE = os.environ.get("SUFFIXE", "")            # ex. "-drive" : plan et annulation séparés
PLAN = os.path.join(HERE, "donnees", f"plan-renommage{SUFFIXE}.json")
UNDO = os.path.join(HERE, "donnees", f"annulation-renommage{SUFFIXE}.json")
IC = os.path.expanduser(os.environ.get("RACINE", tri.IC))   # iCloud par défaut, ou un autre dossier (Drive)
EXCLUS = tuple(os.environ["EXCLUS"].split("|")) if os.environ.get("EXCLUS") else ("00 À trier", "80 Images")
DOCS = (".pdf", ".doc", ".docx", ".odt", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".heic", ".txt", ".rtf", ".mp4", ".mov", ".kmz")
DOCS_SEULEMENT = bool(os.environ.get("DOCS_SEULEMENT"))   # Drive : ne renommer que des documents
MARQUEURS_CODE = {"package.json", "composer.json", "pom.xml", "build.gradle", ".git", "angular.json", "Makefile", ".idea", "src", "app", "www", "vendor", "node_modules"}
PAQUETS = ("_files", "kit-de-modeles", "retours-recette")   # dossiers dont les fichiers se référencent entre eux
LOT = 20

SCHEMA = {"type": "object", "properties": {"fichiers": {"type": "array", "items": {
    "type": "object",
    "properties": {"fichier": {"type": "string"}, "nouveau_nom": {"type": "string"}},
    "required": ["fichier", "nouveau_nom"]}}}, "required": ["fichiers"]}


def lots():
    for r, ds, fs in os.walk(IC):
        if MARQUEURS_CODE & set(ds + fs) or any(x.endswith((".sln", ".csproj", ".xcodeproj")) for x in ds + fs):
            ds[:] = []   # projet de code : on n'y touche pas
            continue
        ds[:] = sorted(d for d in ds if not d.startswith(".") and not d.endswith(PAQUETS))
        rel = os.path.relpath(r, IC)
        if rel == "." or rel.startswith(EXCLUS):
            continue
        fs = sorted(f for f in fs if not f.startswith(".") and (not DOCS_SEULEMENT or f.lower().endswith(DOCS)))
        for i in range(0, len(fs), LOT):
            yield rel, [os.path.join(r, f) for f in fs[i:i + LOT]]


def proposer(rel, files):
    blocs = []
    for p in files:
        tri.materialize(p)
        txt = tri.extract(p)
        name = os.path.basename(p)
        blocs.append(f"### {name}\n" + (f"Extrait :\n{txt}" if txt else "(pas de texte : déduis du nom, ou lis-le avec Read si c'est un PDF ou une image : " + p + ")"))
    prompt = f"""Renomme les fichiers du dossier "{rel}" (documents de {tri.REGLAGES['proprietaire']}) selon cette convention :

{tri.CONVENTION}
Le dossier donne le thème : ne répète pas son nom dans le fichier sauf si c'est l'émetteur.
Si le nom respecte déjà la convention, renvoie-le à l'identique. N'invente pas de date : prends-la dans le contenu ou dans le nom actuel, sinon utilise la forme sans date.
Le champ "fichier" reprend EXACTEMENT le nom donné après "###". Réponds pour chaque fichier.

{chr(10).join(blocs)}
"""
    cmd = [tri.CLAUDE, "-p", prompt, "--model", tri.MODEL, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--tools", "Read", "--allowedTools", "Read",
           "--add-dir", IC, "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
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
            out.append({"de": os.path.relpath(src, IC), "vers": os.path.join(rel, new)})
    return out


def sha(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def plan(prefixe=None):
    todo = [l for l in lots() if not prefixe or l[0].startswith(prefixe)]
    print(f"{sum(len(f) for _, f in todo)} fichiers en {len(todo)} lots", flush=True)
    res = []
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        for i, r in enumerate(ex.map(lambda a: proposer(*a), todo)):
            res += r
            print(f"lot {i + 1}/{len(todo)}", flush=True)
    if prefixe and os.path.exists(PLAN):   # remplace seulement la partie re-planifiée
        res = [e for e in json.load(open(PLAN)) if not e["de"].startswith(prefixe)] + res
    for e in res:
        e.pop("collision_avec", None); e.pop("doublon_exact", None)
    # collisions et doublons exacts
    seen = {}
    for e in res:
        e["identique"] = e["de"] == e["vers"]
        k = e["vers"].lower()
        if k in seen:
            a, b = os.path.join(IC, seen[k]["de"]), os.path.join(IC, e["de"])
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
    print(f"plan : {n} renommages, {sum(1 for e in res if e.get('doublon_exact'))} doublons, "
          f"{sum(1 for e in res if e.get('collision_avec') and not e.get('doublon_exact'))} collisions")


def appliquer():
    res = json.load(open(PLAN))
    undo = []
    # collisions : on garde le plus récent, l'autre va à la Corbeille
    perdants = set()
    for e in res:
        if e.get("collision_avec") and not e.get("doublon_exact"):
            a, b = os.path.join(IC, e["de"]), os.path.join(IC, e["collision_avec"])
            if os.path.exists(a) and os.path.exists(b):
                perdants.add(e["de"] if os.path.getmtime(a) < os.path.getmtime(b) else e["collision_avec"])
    for rel in perdants:
        tri.to_trash(os.path.join(IC, rel))
        undo.append({"corbeille": rel})
    for e in res:
        if e["de"] in perdants:
            continue
        if e["identique"]:
            continue
        src = os.path.join(IC, e["de"])
        if not os.path.exists(src):
            continue
        if e.get("doublon_exact"):
            tri.to_trash(src)
            undo.append({"corbeille": e["de"]})
            continue
        dst = os.path.join(IC, e["vers"])
        meme = lambda x: tri.unicodedata.normalize("NFC", x).lower()
        if meme(dst) != meme(src):   # simple changement de casse ou d'accents : pas un conflit
            dst = tri.unique(dst)
        os.rename(src, dst)
        undo.append({"de": e["de"], "vers": os.path.relpath(dst, IC)})
    json.dump(undo, open(UNDO, "w"), ensure_ascii=False, indent=1)
    print(f"{sum('vers' in u for u in undo)} renommés, {sum('corbeille' in u for u in undo)} doublons à la Corbeille")


def annuler():
    for u in reversed(json.load(open(UNDO))):
        if "vers" in u and os.path.exists(os.path.join(IC, u["vers"])):
            os.rename(os.path.join(IC, u["vers"]), os.path.join(IC, u["de"]))
    print("annulé (les doublons sont dans la Corbeille)")


if __name__ == "__main__":
    {"plan": plan, "appliquer": appliquer, "annuler": annuler}[sys.argv[1]](*sys.argv[2:])
