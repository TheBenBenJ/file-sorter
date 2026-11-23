#!/usr/bin/env python3
"""Tidiness audit, without AI: counts what is not tidy in iCloud and the Drive.

Writes tidiness-state.json (read by the Dashboard). Since 22/11/2026 (ticket 36) nothing in it asks Benjamin for a manual action:
« total » is 0 and the lists the Dashboard shows (vrac, noms, a_trier_vieux, telechargements_vieux) are empty, because a file the sorting
cannot place stays in « 00 À trier » (or in ~/Downloads) on purpose. The loose files and the names outside the convention are kept as
« vrac_info » and « noms_info », for the record.
The archive (« 90 Archives » of iCloud and of the Drive) is not audited: the sorting's review moves there, at the same
path and under the same name, what is no longer current, so a file loose in its theme folder is loose there too, and
nothing in it waits for anyone (Benjamin, 20/11/2026: « ce ne sont que des éléments qui devraient être archivés »).
Reads names and dates only: nothing is downloaded, modified or deleted.
"""

import json
import os
import re
import unicodedata
from datetime import datetime

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(HERE, "settings.json"), encoding="utf-8") as _f:
    SETTINGS = json.load(_f)
ROOTS = {
    "iCloud": os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs"),
    "Drive": os.path.join(HOME, SETTINGS["drive"]),
}
OUTPUT = os.path.join(HERE, "tidiness-state.json")

# intended exceptions (see the cloud-tidying skill)
BUNDLES = tuple(SETTINGS["paquets"])
LOOSE_ALLOWED = set(SETTINGS["vrac_autorise"])
# the archive, as sort.py finds it: an « NN Archives » at a root, or the settings' name
ARCHIVES, ARCHIVE_NAME = SETTINGS.get("archives") or "90 Archives", re.compile(r"\d\d Archives?", re.I)
# suspicious names; the French words match the French names of scans and downloads
SUSPECT_NAME = re.compile(
    r"_|\(\d+\)|[0-9a-f]{8}-[0-9a-f]{4}|compressed|^IMG_\d|^(?!.*[a-zà-ÿ]).{8,}\.\w+$|^(Document|Scan|Numériser|image|téléchargement)\b",
    re.I,
)
CODE_MARKERS = {
    "package.json",
    "composer.json",
    "pom.xml",
    ".git",
    "angular.json",
    "src",
    "app",
    "vendor",
    "node_modules",
}


def audit():
    loose, names = [], []
    for label, root in ROOTS.items():
        if not os.path.isdir(root):
            continue
        for r, ds, fs in os.walk(root):
            ds[:] = [d for d in ds if not d.startswith(".")]
            rel = unicodedata.normalize("NFC", os.path.relpath(r, root))  # the Mac decomposes accents
            if rel == ".":  # the archive: what the review moved there keeps its path and its name, nothing to tidy
                ds[:] = [
                    d
                    for d in ds
                    if not ARCHIVE_NAME.fullmatch(unicodedata.normalize("NFC", d))
                    and unicodedata.normalize("NFC", d) != ARCHIVES
                ]
            if CODE_MARKERS & set(ds + fs):
                ds[:] = []
                continue
            fs = [f for f in fs if not f.startswith(".") and not f.endswith((".gscript",))]
            if rel.startswith("00 "):  # an inbox: where the sorting leaves what it cannot place
                continue
            if any(p in "/" + rel for p in BUNDLES):
                continue
            if rel != "." and ds and fs and rel not in LOOSE_ALLOWED:
                loose += [f"{label}/{rel}/{f}" for f in fs]
            if rel == "." and fs:
                loose += [f"{label}/{f}" for f in fs]
            names += [f"{label}/{rel}/{f}" for f in fs if SUSPECT_NAME.search(f)]
    # No alert for something to do by hand (Benjamin, ticket 36, 22/11/2026: « si on ne trouve pas où ranger, on met dans à trier »): what waits
    # in an inbox or in ~/Downloads is where the sorting leaves what it cannot place, so it is no longer reported, and the loose files and
    # the doubtful names are kept in the state for the record only (the review archives what is no longer current by itself).
    # The keys are read by the Dashboard: they stay as they are (French, like the others); « total » is what the Dashboard shows as to do.
    return {
        "date": datetime.now().isoformat(timespec="minutes"),
        "vrac": [],
        "noms": [],
        "a_trier_vieux": [],
        "telechargements_vieux": [],
        "total": 0,
        "vrac_info": loose,
        "noms_info": names,
    }


if __name__ == "__main__":
    state = audit()
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, OUTPUT)
    print(
        f"nothing to do by hand; for the record: {len(state['vrac_info'])} loose, {len(state['noms_info'])} with a name outside the convention"
    )
