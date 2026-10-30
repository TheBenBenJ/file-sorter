#!/usr/bin/env python3
"""Audit de rangement, sans IA : compte ce qui n'est pas rangé dans iCloud et le Drive.

Écrit etat-rangement.json (lu par le Dashboard) : fichiers en vrac à côté de sous-dossiers,
noms hors convention, fichiers qui traînent dans « 00 À trier » depuis plus de 7 jours.
Ne lit que les noms et les dates : rien n'est téléchargé ni modifié.
"""
import json, os, re, time, unicodedata
from datetime import datetime

HOME = os.path.expanduser("~")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "reglages.json"), encoding="utf-8") as f:
    REGLAGES = json.load(f)
RACINES = {
    "iCloud": os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs"),
    "Drive": os.path.join(HOME, REGLAGES["drive"]),
}
SORTIE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etat-rangement.json")

# exceptions voulues (cf. skill rangement-cloud)
PAQUETS = tuple(REGLAGES["paquets"])
VRAC_AUTORISE = set(REGLAGES["vrac_autorise"])
NOM_SUSPECT = re.compile(r"_|\(\d+\)|[0-9a-f]{8}-[0-9a-f]{4}|compressed|^IMG_\d|^(?!.*[a-zà-ÿ]).{8,}\.\w+$|^(Document|Scan|Numériser|image|téléchargement)\b", re.I)
CODE = {"package.json", "composer.json", "pom.xml", ".git", "angular.json", "src", "app", "vendor", "node_modules"}
AJOUR = 7 * 86400


def auditer():
    vrac, noms, vieux = [], [], []
    now = time.time()
    for lib, racine in RACINES.items():
        if not os.path.isdir(racine):
            continue
        for r, ds, fs in os.walk(racine):
            ds[:] = [d for d in ds if not d.startswith(".")]
            rel = unicodedata.normalize("NFC", os.path.relpath(r, racine))   # accents décomposés côté Mac
            if CODE & set(ds + fs):
                ds[:] = []
                continue
            fs = [f for f in fs if not f.startswith(".") and not f.endswith((".gscript",))]
            if rel.startswith("00 "):
                for f in fs:
                    try:
                        if now - max(os.stat(os.path.join(r, f)).st_mtime, os.stat(os.path.join(r, f)).st_ctime) > AJOUR:
                            vieux.append(f"{lib}/{rel}/{f}")
                    except OSError:
                        pass
                continue
            if any(p in "/" + rel for p in PAQUETS):
                continue
            if rel != "." and ds and fs and rel not in VRAC_AUTORISE:
                vrac += [f"{lib}/{rel}/{f}" for f in fs]
            if rel == "." and fs:
                vrac += [f"{lib}/{f}" for f in fs]
            noms += [f"{lib}/{rel}/{f}" for f in fs if NOM_SUSPECT.search(f)]
    return {"date": datetime.now().isoformat(timespec="minutes"), "vrac": vrac, "noms": noms, "a_trier_vieux": vieux,
            "total": len(set(vrac) | set(noms) | set(vieux))}


if __name__ == "__main__":
    etat = auditer()
    tmp = SORTIE + ".tmp"
    json.dump(etat, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, SORTIE)
    print(f"{etat['total']} fichier(s) à revoir : {len(etat['vrac'])} en vrac, {len(etat['noms'])} noms, {len(etat['a_trier_vieux'])} dans À trier depuis > 7 j")
