#!/usr/bin/env python3
"""Applique un plan de rangement (JSON produit par l'audit d'un dossier) dans iCloud.

  appliquer-plan.py verifier plan.json [plan2.json …]   -> contrôle, ne touche à rien
  appliquer-plan.py appliquer plan.json [plan2.json …]  -> déplace / renomme / met à la corbeille, journal d'annulation
  appliquer-plan.py annuler journal.json                -> remet les fichiers déplacés à leur place
"""
import json, os, shutil, subprocess, sys, unicodedata
from datetime import datetime

IC = os.path.expanduser(os.environ.get("RACINE", "~/Library/Mobile Documents/com~apple~CloudDocs"))   # RACINE=… pour le Drive
HERE = os.path.dirname(os.path.abspath(__file__))
nfc = lambda s: unicodedata.normalize("NFC", s)


def chemin(rel):
    p = os.path.normpath(os.path.join(IC, rel))
    if not p.startswith(IC + os.sep):
        raise ValueError(f"hors d'iCloud : {rel}")
    return p


def existe(rel):
    """Le Mac stocke les noms décomposés : on compare en NFC."""
    p = chemin(rel)
    if os.path.exists(p):
        return p
    d, f = os.path.split(p)
    if os.path.isdir(d):
        for x in os.listdir(d):
            if nfc(x) == nfc(f):
                return os.path.join(d, x)
    return None


def charger(fichiers):
    ops = []
    for f in fichiers:
        ops += json.load(open(f))["operations"]
    return ops


def verifier(ops):
    erreurs, dsts, srcs = [], set(), set()
    for o in ops:
        s = nfc(o["src"])
        if s in srcs:
            erreurs.append(f"source en double : {s}")
        srcs.add(s)
        if not existe(o["src"]):
            erreurs.append(f"source absente : {s}")
        if o["op"] == "move":
            d = nfc(o["dst"])
            if d in dsts:
                erreurs.append(f"destination en double : {d}")
            dsts.add(d)
            if existe(o["dst"]) and nfc(o["dst"]).lower() != s.lower() and nfc(o["dst"]) not in srcs:   # simple changement de casse : pas un conflit
                erreurs.append(f"écraserait : {d}")
        elif o["op"] != "trash":
            erreurs.append(f"opération inconnue : {o}")
    return erreurs


def corbeille(p):
    subprocess.run(["trash", p], capture_output=True)
    if os.path.exists(p):
        cible = os.path.join(os.path.expanduser("~/.Trash"), os.path.basename(p))
        if os.path.exists(cible):
            cible += f" {datetime.now():%H%M%S}"
        shutil.move(p, cible)


def appliquer(ops):
    journal = []
    # 1. corbeille, 2. déplacements via un nom temporaire (évite les conflits d'échange de noms)
    for o in ops:
        if o["op"] == "trash":
            p = existe(o["src"])
            if p:
                corbeille(p)
                journal.append({"corbeille": o["src"]})
    temp = []
    for i, o in enumerate(ops):
        if o["op"] == "move":
            p = existe(o["src"])
            if not p:
                continue
            t = p + f".deplacement-{i}"
            os.rename(p, t)
            temp.append((t, o))
    for t, o in temp:
        d = chemin(o["dst"])
        os.makedirs(os.path.dirname(d), exist_ok=True)
        os.rename(t, d)
        journal.append({"de": o["src"], "vers": o["dst"]})
    # dossiers vidés
    for r, ds, fs in sorted(os.walk(IC), key=lambda x: -len(x[0])):
        if r == IC or "/." in r or os.path.dirname(r) == IC or os.path.basename(r).startswith("00 "):
            continue   # jamais un dossier de 1er niveau ni une boîte d'entrée « 00 … », même vide
        restants = [f for f in os.listdir(r) if f not in (".DS_Store",)]
        if not restants:
            if os.path.exists(os.path.join(r, ".DS_Store")):
                os.remove(os.path.join(r, ".DS_Store"))
            os.rmdir(r)
    nom = os.path.join(HERE, "donnees", f"annulation-rangement-{datetime.now():%Y%m%d-%H%M%S}.json")
    json.dump(journal, open(nom, "w"), ensure_ascii=False, indent=1)
    print(f"{sum('vers' in j for j in journal)} déplacés/renommés, {sum('corbeille' in j for j in journal)} à la corbeille ; annulation : {nom}")


def annuler(journal):
    for j in reversed(json.load(open(journal))):
        if "vers" in j and existe(j["vers"]):
            d = chemin(j["de"])
            os.makedirs(os.path.dirname(d), exist_ok=True)
            os.rename(existe(j["vers"]), d)
    print("annulé (la corbeille n'est pas restaurée automatiquement)")


if __name__ == "__main__":
    action, args = sys.argv[1], sys.argv[2:]
    if action == "annuler":
        annuler(args[0])
        sys.exit()
    ops = charger(args)
    err = verifier(ops)
    print(f"{len(ops)} opérations, {len(err)} problème(s)")
    for e in err:
        print("  ", e)
    if action == "appliquer":
        if err:
            sys.exit("plan refusé : corriger d'abord")
        appliquer(ops)
