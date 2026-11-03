#!/usr/bin/env python3
"""Applies a tidying plan (JSON produced by the audit of a folder) in iCloud.

  appliquer-plan.py verifier plan.json [plan2.json …]   -> checks, touches nothing
  appliquer-plan.py appliquer plan.json [plan2.json …]  -> moves / renames / sends to the Trash, writes an undo journal
  appliquer-plan.py annuler journal.json                -> puts the moved files back
RACINE=<folder> works on another root than iCloud (the Drive, for example).
"""
import json, os, shutil, subprocess, sys, unicodedata
from datetime import datetime

ROOT = os.path.expanduser(os.environ.get("RACINE", "~/Library/Mobile Documents/com~apple~CloudDocs"))   # RACINE=… for the Drive
HERE = os.path.dirname(os.path.abspath(__file__))
nfc = lambda s: unicodedata.normalize("NFC", s)


def resolve(rel):
    p = os.path.normpath(os.path.join(ROOT, rel))
    if not p.startswith(ROOT + os.sep):
        raise ValueError(f"outside iCloud: {rel}")
    return p


def find(rel):
    """The Mac stores decomposed names: compare in NFC."""
    p = resolve(rel)
    if os.path.exists(p):
        return p
    d, f = os.path.split(p)
    if os.path.isdir(d):
        for x in os.listdir(d):
            if nfc(x) == nfc(f):
                return os.path.join(d, x)
    return None


def load(files):
    ops = []
    for f in files:
        ops += json.load(open(f))["operations"]
    return ops


def check(ops):
    errors, dsts, srcs = [], set(), set()
    for o in ops:
        s = nfc(o["src"])
        if s in srcs:
            errors.append(f"duplicate source: {s}")
        srcs.add(s)
        if not find(o["src"]):
            errors.append(f"missing source: {s}")
        if o["op"] == "move":
            d = nfc(o["dst"])
            if d in dsts:
                errors.append(f"duplicate destination: {d}")
            dsts.add(d)
            if find(o["dst"]) and nfc(o["dst"]).lower() != s.lower() and nfc(o["dst"]) not in srcs:   # a mere change of case: not a conflict
                errors.append(f"would overwrite: {d}")
        elif o["op"] != "trash":
            errors.append(f"unknown operation: {o}")
    return errors


def to_trash(p):
    subprocess.run(["trash", p], capture_output=True)
    if os.path.exists(p):
        target = os.path.join(os.path.expanduser("~/.Trash"), os.path.basename(p))
        if os.path.exists(target):
            target += f" {datetime.now():%H%M%S}"
        shutil.move(p, target)


def apply_ops(ops):
    journal = []   # undo journal; its French keys (corbeille, de, vers) are read back by `annuler`
    # 1. Trash, 2. moves through a temporary name (avoids conflicts when two names are swapped)
    for o in ops:
        if o["op"] == "trash":
            p = find(o["src"])
            if p:
                to_trash(p)
                journal.append({"corbeille": o["src"]})
    temp = []
    for i, o in enumerate(ops):
        if o["op"] == "move":
            p = find(o["src"])
            if not p:
                continue
            t = p + f".deplacement-{i}"
            os.rename(p, t)
            temp.append((t, o))
    for t, o in temp:
        d = resolve(o["dst"])
        os.makedirs(os.path.dirname(d), exist_ok=True)
        os.rename(t, d)
        journal.append({"de": o["src"], "vers": o["dst"]})
    # emptied folders
    for r, ds, fs in sorted(os.walk(ROOT), key=lambda x: -len(x[0])):
        if r == ROOT or "/." in r or os.path.dirname(r) == ROOT or os.path.basename(r).startswith("00 "):
            continue   # never a first-level folder nor a « 00 … » inbox, even empty
        left = [f for f in os.listdir(r) if f not in (".DS_Store",)]
        if not left:
            if os.path.exists(os.path.join(r, ".DS_Store")):
                os.remove(os.path.join(r, ".DS_Store"))
            os.rmdir(r)
    name = os.path.join(HERE, "donnees", f"annulation-rangement-{datetime.now():%Y%m%d-%H%M%S}.json")
    json.dump(journal, open(name, "w"), ensure_ascii=False, indent=1)
    print(f"{sum('vers' in j for j in journal)} moved/renamed, {sum('corbeille' in j for j in journal)} sent to the Trash; undo: {name}")


def undo(journal):
    for j in reversed(json.load(open(journal))):
        if "vers" in j and find(j["vers"]):
            d = resolve(j["de"])
            os.makedirs(os.path.dirname(d), exist_ok=True)
            os.rename(find(j["vers"]), d)
    print("undone (the Trash is not restored automatically)")


if __name__ == "__main__":
    # the command words stay French: the rangement-cloud skill uses them
    command, args = sys.argv[1], sys.argv[2:]
    if command == "annuler":
        undo(args[0])
        sys.exit()
    ops = load(args)
    errors = check(ops)
    print(f"{len(ops)} operations, {len(errors)} problem(s)")
    for e in errors:
        print("  ", e)
    if command == "appliquer":
        if errors:
            sys.exit("plan refused: fix it first")
        apply_ops(ops)
