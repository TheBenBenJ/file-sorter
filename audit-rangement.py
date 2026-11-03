#!/usr/bin/env python3
"""Tidiness audit, without AI: counts what is not tidy in iCloud and the Drive.

Writes etat-rangement.json (read by the Dashboard): loose files next to subfolders,
names outside the convention, files lying in « 00 À trier » for more than 7 days.
Reads names and dates only: nothing is downloaded or modified.
"""
import json, os, re, time, unicodedata
from datetime import datetime

HOME = os.path.expanduser("~")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "reglages.json"), encoding="utf-8") as f:
    SETTINGS = json.load(f)
ROOTS = {
    "iCloud": os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs"),
    "Drive": os.path.join(HOME, SETTINGS["drive"]),
}
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etat-rangement.json")

# intended exceptions (see the rangement-cloud skill)
BUNDLES = tuple(SETTINGS["paquets"])
LOOSE_ALLOWED = set(SETTINGS["vrac_autorise"])
# suspicious names; the French words match the French names of scans and downloads
SUSPECT_NAME = re.compile(r"_|\(\d+\)|[0-9a-f]{8}-[0-9a-f]{4}|compressed|^IMG_\d|^(?!.*[a-zà-ÿ]).{8,}\.\w+$|^(Document|Scan|Numériser|image|téléchargement)\b", re.I)
CODE_MARKERS = {"package.json", "composer.json", "pom.xml", ".git", "angular.json", "src", "app", "vendor", "node_modules"}
STALE_AFTER = 7 * 86400


def audit():
    loose, names, stale = [], [], []
    now = time.time()
    for label, root in ROOTS.items():
        if not os.path.isdir(root):
            continue
        for r, ds, fs in os.walk(root):
            ds[:] = [d for d in ds if not d.startswith(".")]
            rel = unicodedata.normalize("NFC", os.path.relpath(r, root))   # the Mac decomposes accents
            if CODE_MARKERS & set(ds + fs):
                ds[:] = []
                continue
            fs = [f for f in fs if not f.startswith(".") and not f.endswith((".gscript",))]
            if rel.startswith("00 "):
                for f in fs:
                    try:
                        if now - max(os.stat(os.path.join(r, f)).st_mtime, os.stat(os.path.join(r, f)).st_ctime) > STALE_AFTER:
                            stale.append(f"{label}/{rel}/{f}")
                    except OSError:
                        pass
                continue
            if any(p in "/" + rel for p in BUNDLES):
                continue
            if rel != "." and ds and fs and rel not in LOOSE_ALLOWED:
                loose += [f"{label}/{rel}/{f}" for f in fs]
            if rel == "." and fs:
                loose += [f"{label}/{f}" for f in fs]
            names += [f"{label}/{rel}/{f}" for f in fs if SUSPECT_NAME.search(f)]
    # the keys are read by the Dashboard: they stay as they are
    return {"date": datetime.now().isoformat(timespec="minutes"), "vrac": loose, "noms": names, "a_trier_vieux": stale,
            "total": len(set(loose) | set(names) | set(stale))}


if __name__ == "__main__":
    state = audit()
    tmp = OUTPUT + ".tmp"
    json.dump(state, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, OUTPUT)
    print(f"{state['total']} file(s) to review: {len(state['vrac'])} loose, {len(state['noms'])} badly named, {len(state['a_trier_vieux'])} in À trier for more than 7 days")
