"""Execution modes: full Mac sorting, server --decide, Mac --apply, and --deposit."""

from datetime import datetime
import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time

import activity

from sorter.config import (
    APPLIED,
    ATTACH_INBOX,
    CHROME_INBOXES,
    CLEANUP,
    DATA,
    DOCUMENTS,
    DOWNLOADS,
    DRIVE,
    DRIVE_INBOX,
    DRIVE_INBOXES,
    DRIVE_RCLONE,
    DRY,
    EXTERNAL,
    GMAIL_INBOX,
    HERE,
    ICLOUD,
    INBOX,
    LOCK,
    MAX_SIZE,
    MIN_AGE,
    OFFICE,
    PARTIAL_EXT,
    PLAN,
    REPORT,
    RESULTS,
    REVIEW_BILAN,
    REVIEW_KINDS,
    REVIEW_NO_TRASH,
    REVIEW_PAUSE,
    ROLE,
    SETTINGS,
    THEME,
    UNREADABLE,
    archive_of,
    is_archive,
    log,
    nfc,
    notify,
    read_json,
    write_json,
)
from sorter.drive import DriveError, drive_move, rclone
from sorter.files import (
    dedupe,
    load_kept,
    make_action,
    materialize,
    pending,
    place,
    relative,
    save_kept,
    sig,
    to_trash,
    twin,
    unique,
    where,
)
from sorter.journal import DRIVE_WHAT, MAC_FINE, downloads_events, filed, kept_why, mac_event, public_reason
from sorter.models import choose
from sorter.review import PHOTOS, Review

DOWNLOADS_SEEN = os.path.join(DATA, "downloads.json")


def audit():
    """Run tidiness audit for the Dashboard (no AI, names and dates only)."""
    subprocess.run([sys.executable, "-I", os.path.join(HERE, "audit.py")], capture_output=True, timeout=300)


def batch(files, moved, kept):
    choices, failure, _ = choose(files)
    for src, dest, new, conf, answer, _ai in choices:
        name = os.path.basename(src)
        if dest is None:
            if os.path.dirname(src) != ATTACH_INBOX:
                kept.append(name)
            if not DRY and os.path.dirname(src) in EXTERNAL:
                os.makedirs(DRIVE_INBOX, exist_ok=True)
                target = unique(os.path.join(DRIVE_INBOX, name))
                shutil.move(src, target)
                save_kept(target)
            elif not DRY:
                save_kept(src)
            log(f"GARDÉ   {name}  (confidence {conf:.2f}, proposed: {answer.get('destination')}) — {answer.get('raison')}")
            continue
        base = DRIVE if name.lower().endswith(OFFICE) else ICLOUD
        target = unique(os.path.join(base, dest, new))
        if DRY:
            log(f"[dry run] {name}  ->  {os.path.relpath(target, ICLOUD)}  ({conf:.2f}) — {answer.get('raison')}")
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(src, target)
        moved.append(f"{new} → {dest}")
        prefix = "Drive/" if base == DRIVE else ""
        log(f"RANGÉ   {name}  ->  {prefix}{os.path.relpath(target, base)}  ({conf:.2f}) — {answer.get('raison')}")
    if failure:
        raise failure


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            for path in files:
                if materialize(path):
                    ready.append(path)
                else:
                    log(f"ATTENTE {os.path.basename(path)}  (not downloaded from iCloud yet, retried on the next pass)")
            files = dedupe(ready)[0]
            if files:
                batch(files, moved, kept)
        if moved or kept:
            msg = f"{len(moved)} rangé(s)" + (f", {len(kept)} laissé(s) dans À trier" if kept else "")
            if len(moved) == 1:
                msg = moved[0]
            notify(msg)
    except Exception as exc:
        log(f"ERREUR  {exc}")
        notify(f"Erreur du tri : {exc}"[:200])
    finally:
        lock.close()
    audit()


def stopped(signum, _frame):
    REPORT.fail("arrêté avant la fin : délai dépassé ou arrêt du serveur")
    activity.failed("passage du tri", "passage arrêté avant la fin : délai dépassé ou arrêt du serveur")
    sys.exit(128 + signum)


def decide():
    """Server mode: decide inbox filing, execute Drive moves via rclone, run folder review, write plan.json."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    signal.signal(signal.SIGTERM, stopped)
    failures, in_drive, later, failure = [], [], [], None
    try:
        REPORT.running("inboxes")
        plan = read_json(PLAN, {"actions": [], "faites": {}})
        results = read_json(RESULTS, {})
        returned = results.get("appliques", {})
        done = {k: v for k, v in plan.get("faites", {}).items() if time.time() - v[1] < 3600}
        actions, came_back, reviewed = [], [], []
        try:
            review = Review(actions, later)
        except Exception as exc:
            review = None
            log(f"ERREUR  review: {type(exc).__name__} {exc}")
        withdrawn = review.withdrawn if review else {}

        def came(action):
            done[f"{action['source']['racine']}|{action['source']['chemin']}"] = [returned[action["id"]], time.time()]
            log(f"MAC     {action['type']} {action['source']['chemin']} : {returned[action['id']]}")
            came_back.append(returned[action["id"]])
            later.append(mac_event(action, returned[action["id"]]))
            if action.get("type") in REVIEW_KINDS:
                reviewed.append((action, returned[action["id"]]))
            withdrawn.pop(action["id"], None)

        for action in plan.get("actions", []):
            if action["id"] in returned:
                came(action)
            elif action.get("type") in REVIEW_KINDS and CLEANUP != "on":
                withdrawn[action["id"]] = {"action": action, "time": time.time()}
                later.append((
                    "annulé",
                    os.path.basename(action["source"]["chemin"]),
                    {
                        "src": place(action["source"]["racine"], os.path.dirname(action["source"]["chemin"])),
                        "detail": f"relecture {'arrêtée' if CLEANUP == 'off' else 'revenue à son essai'} : retiré du plan, le Mac ne le fera pas",
                    },
                ))
                log(f"review: {action['type']} {action['source']['chemin']} withdrawn from the Mac's plan (FILE_SORTER_CLEANUP={CLEANUP})")
            elif time.time() - action.get("cree_le", 0) < 7 * 86400:
                actions.append(action)
                withdrawn.pop(action["id"], None)

        plan_ids = {a["id"] for a in plan.get("actions", [])}
        for ident, item in list(withdrawn.items()):
            if ident in returned and ident not in plan_ids:
                came(item["action"])
            elif time.time() - item.get("time", 0) > 8 * 86400:
                del withdrawn[ident]

        if came_back:
            errors = sum(status not in MAC_FINE for status in came_back)
            REPORT.set("icloud", *(("failed", f"le Mac n'a pas pu appliquer {errors} action(s) du plan") if errors else ("ok",)))
        elif not actions:
            REPORT.set("icloud", "skipped")

        planned = {f"{a['source']['racine']}|{a['source']['chemin']}" for a in actions} | set(done)
        files = pending(
            (GMAIL_INBOX, DRIVE_INBOX) + CHROME_INBOXES + (DRIVE, INBOX, ATTACH_INBOX),
            skip=lambda p: "|".join(relative(p)) in planned,
        )
        server_side = lambda p: bool(DRIVE_RCLONE) and os.path.dirname(p) in DRIVE_INBOXES

        def by_server(path, what, act):
            REPORT.running("drive")
            try:
                result = act()
                done["|".join(relative(path))] = [f"ok (serveur) : {result}", time.time()]
                in_drive.append(path)
                return result
            except DriveError as exc:
                failures.append(os.path.basename(path))
                log(f"ERREUR  {what} {os.path.basename(path)} : {exc}")
                activity.log(
                    "échec",
                    os.path.basename(path),
                    src=where(os.path.dirname(path)),
                    detail=f"{DRIVE_WHAT.get(what, what)} impossible, nouvel essai au prochain passage : {exc}",
                )
                return None

        found, duplicates = bool(files), []
        if files:
            files, duplicates = dedupe(files)
        REPORT.set(
            "inboxes",
            *(
                ("failed", "arrivée illisible pour l'instant : " + ", ".join(UNREADABLE))
                if UNREADABLE
                else ("ok",)
                if found
                else ("skipped",)
            ),
        )

        for path, orig in duplicates:
            name = os.path.basename(path)
            if server_side(path):
                if by_server(
                    path,
                    "doublon",
                    lambda p=path: rclone("deletefile", "--drive-use-trash", DRIVE_RCLONE + os.path.relpath(p, DRIVE)) or "corbeille du Drive",
                ):
                    log(f"DOUBLON {name}  (identical to {orig}) -> the Drive's trash, by the server")
                    activity.log(
                        "doublon",
                        name,
                        src=where(os.path.dirname(path)),
                        dst="corbeille du Drive",
                        detail=f"identique à {twin(orig)} : mis à la corbeille du Drive par le serveur",
                    )
            else:
                actions.append(make_action("doublon", path, original=nfc(orig)))
                log(f"DOUBLON {name}  (identical to {orig}) -> Trash, by the Mac")
                later.append((
                    "doublon",
                    name,
                    {
                        "src": where(os.path.dirname(path)),
                        "dst": "Corbeille du Mac",
                        "detail": f"identique à {twin(orig)} : le Mac le met à la Corbeille à son prochain passage",
                    },
                ))

        if not files:
            REPORT.set("decide", "skipped")
        else:
            REPORT.running("decide")
            choices, failure, waiting = choose(files)
            REPORT.set(
                "decide",
                *(
                    ("failed", f"le modèle local n'a pas répondu pour {waiting} fichier(s) : nouvel essai au prochain passage")
                    if waiting
                    else ("ok",)
                ),
            )
            for src, dest, new, conf, answer, model in choices:
                name, origin = os.path.basename(src), where(os.path.dirname(src))
                ai = {**model, "confidence": conf}
                if dest is None:
                    signature = sig(src)
                    why = kept_why(conf, answer)
                    if os.path.dirname(src) in (GMAIL_INBOX,) + CHROME_INBOXES + (DRIVE,):
                        if server_side(src):
                            moved = by_server(src, "vers 00 À trier", lambda s=src, n=name: drive_move(s, os.path.relpath(DRIVE_INBOX, DRIVE), n))
                            if not moved:
                                continue
                            activity.log("à trier", name, src=origin, dst=place("drive", moved), detail=f"{why} ; mis dans 00 À trier par le serveur", **ai)
                        else:
                            actions.append(make_action("vers_a_trier", src))
                            later.append((
                                "à trier",
                                name,
                                {"src": origin, "dst": where(DRIVE_INBOX), "detail": f"{why} ; le Mac le met dans 00 À trier à son prochain passage", **ai},
                            ))
                    else:
                        activity.log("gardé", name, src=origin, detail=why, **ai)
                    save_kept(src, signature)
                    log(f"GARDÉ   {name}  (confidence {conf:.2f}, proposed: {answer.get('destination')}) — {answer.get('raison')}")
                    continue
                base = "drive" if name.lower().endswith(OFFICE) else "icloud"
                if base == "drive" and server_side(src):
                    target = by_server(src, "rangement", lambda s=src, d=dest, n=new: drive_move(s, d, n))
                    if target:
                        log(f"RANGÉ   {name}  ->  Drive/{target}  ({conf:.2f}) — {answer.get('raison')} (by the server)")
                        for act_name, obj, fields in filed(name, origin, place("drive", target), answer, ai, "rangé par le serveur dans le Drive"):
                            activity.log(act_name, obj, **fields)
                    continue
                actions.append(
                    make_action(
                        "ranger",
                        src,
                        cible={"racine": base, "chemin": nfc(os.path.join(dest, new))},
                        confiance=conf,
                        raison=answer.get("raison", ""),
                    )
                )
                prefix = "Drive/" if base == "drive" else ""
                log(f"PLAN    {name}  ->  {prefix}{os.path.join(dest, new)}  ({conf:.2f}) — {answer.get('raison')}")
                later += filed(name, origin, place(base, nfc(os.path.join(dest, new))), answer, ai, "le Mac le range à son prochain passage")

        if review:
            try:
                review.planned = {f"{a['source']['racine']}|{a['source']['chemin']}" for a in actions}
                review.by_server = by_server
                for action, status in reviewed:
                    review.returned(action, status)
                if CLEANUP == "on":
                    review.apply_pending()
                if CLEANUP in ("trial", "on") and not files:
                    review.walk()
                if CLEANUP in ("trial", "on"):
                    try:
                        review.bilan()
                    except Exception as exc:
                        review.state["bilan"] = REVIEW_BILAN
                        log(f"ERREUR  review: bilan {type(exc).__name__} {exc}")
            except Exception as exc:
                review.state["pause_until"] = time.time() + REVIEW_PAUSE
                log(f"ERREUR  review: {type(exc).__name__} {exc}")
                REPORT.set("decide", "failed", f"relecture des dossiers en pause une heure : erreur {type(exc).__name__}")
                activity.log("échec", "relecture des dossiers", detail=f"en pause une heure : {type(exc).__name__} : {exc}")

        REPORT.set(
            "drive",
            *(
                ("failed", f"{len(failures)} déplacement(s) dans le Drive en échec, nouvel essai au prochain passage")
                if failures
                else ("ok",)
                if in_drive
                else ("skipped",)
            ),
        )
        REPORT.running("plan")
        write_json(PLAN, {"genere_a": datetime.now().isoformat(timespec="seconds"), "actions": actions, "faites": done})
        if review and review.changed():
            try:
                review.save()
            except OSError as exc:
                log(f"ERREUR  review.json not written: {exc}")
        REPORT.set("plan", "ok" if actions else "skipped")
        for act_name, obj, fields in later + downloads_events(results.get("telechargements") or []):
            activity.log(act_name, obj, **fields)
        if failure:
            REPORT.running("decide")
            raise failure
        activity.recovered("passage du tri")
    except Exception as exc:
        log(f"ERREUR  {exc}")
        reason = public_reason(exc, REPORT.current())
        REPORT.fail(reason)
        activity.failed("passage du tri", f"{reason} ; {type(exc).__name__} : {exc}")
        notify("Erreur du tri des fichiers sur le serveur : voir son journal")
        raise
    finally:
        lock.close()
    REPORT.finish()
    if failures:
        log(f"ERREUR  {len(failures)} move(s) in the Drive failed, retried on the next pass: {', '.join(failures)}")
        sys.exit(1)


def prune(folder, base):
    """Remove folders emptied by archiving or deletion (never top-level or level-2 theme folders)."""
    while len(nfc(os.path.relpath(folder, base)).split(os.sep)) > 2 and not os.path.relpath(folder, base).startswith(".."):
        try:
            if [f for f in os.listdir(folder) if f != ".DS_Store"]:
                return
            ds_store = os.path.join(folder, ".DS_Store")
            if os.path.exists(ds_store):
                os.remove(ds_store)
            os.rmdir(folder)
        except OSError:
            return
        log(f"VIDÉ    {os.path.relpath(folder, base)} (empty folder removed)")
        folder = os.path.dirname(folder)


def apply_action(action, moved, kept):
    """Execute one validated action from plan.json on the Mac."""
    roots = {"icloud": ICLOUD, "drive": DRIVE}

    def resolve(spec):
        base = roots[spec["racine"]]
        resolved = os.path.normpath(os.path.join(base, nfc(spec["chemin"])))
        if not resolved.startswith(base + os.sep) or ".." in spec["chemin"].split("/"):
            raise ValueError("chemin hors d'iCloud et du Drive")
        return resolved

    if action["type"] == "deposer":
        if action["cible"]["chemin"].startswith("00 "):
            raise ValueError("destination dans une boîte d'arrivée")
        target = unique(resolve(action["cible"]))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(action["contenu"])
        moved.append(f"{os.path.basename(target)} → {os.path.dirname(action['cible']['chemin'])}")
        log(f"DÉPOSÉ  {os.path.relpath(target, roots[action['cible']['racine']])}")
        return "ok"

    src = resolve(action["source"])
    if action["type"] in REVIEW_KINDS:
        top = nfc(action["source"]["chemin"]).split("/")
        if len(top) < 2 or not THEME.match(top[0]) or top[0].startswith("00 ") or is_archive(top[0]):
            raise ValueError("source hors des dossiers par thèmes")
        if any(b in "/" + "/".join(top[:-1]) for b in tuple(SETTINGS.get("paquets") or ()) + (PHOTOS,)):
            raise ValueError("source dans un dossier que la relecture ne touche pas")
    elif nfc(os.path.dirname(src)) not in tuple(nfc(x) for x in (INBOX, GMAIL_INBOX, DRIVE_INBOX, ATTACH_INBOX) + CHROME_INBOXES + (DRIVE,)):
        raise ValueError("source hors des boîtes d'arrivée")

    if not os.path.exists(src):
        return "absent"
    if action["type"] not in REVIEW_KINDS and not materialize(src):
        return None
    if os.path.getsize(src) != action["taille"]:
        return "modifié depuis la décision"

    name = os.path.basename(src)
    if action["type"] == "doublon":
        to_trash(src)
        log(f"DOUBLON {name}  (identical to {action.get('original')}) -> Trash")
    elif action["type"] == "vers_a_trier":
        shutil.move(src, unique(os.path.join(DRIVE_INBOX, name)))
        kept.append(name)
        log(f"GARDÉ   {name}  -> 00 À trier (server's decision)")
    elif action["type"] == "ranger":
        if action["cible"]["chemin"].startswith("00 "):
            raise ValueError("destination dans une boîte d'arrivée")
        target = unique(resolve(action["cible"]))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(src, target)
        dest = os.path.dirname(action["cible"]["chemin"])
        moved.append(f"{os.path.basename(target)} → {dest}")
        prefix = "Drive/" if action["cible"]["racine"] == "drive" else ""
        log(f"RANGÉ   {name}  ->  {prefix}{os.path.relpath(target, roots[action['cible']['racine']])}  ({action.get('confiance', 0):.2f}) — {action.get('raison', '')}")
    elif action["type"] == "archiver":
        if action["cible"]["racine"] != action["source"]["racine"] or not is_archive(nfc(action["cible"]["chemin"]).split("/")[0]):
            raise ValueError("destination hors des archives")
        target = unique(resolve(action["cible"]))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(src, target)
        prune(os.path.dirname(src), roots[action["source"]["racine"]])
        prefix = "Drive/" if action["cible"]["racine"] == "drive" else ""
        log(f"ARCHIVÉ {name}  ->  {prefix}{os.path.relpath(target, roots[action['cible']['racine']])} — {action.get('raison', '')}")
    elif action["type"] == "supprimer":
        if action["source"]["racine"] in REVIEW_NO_TRASH:
            msg = "rien n'est mis à la corbeille dans le Drive" if action["source"]["racine"] == "drive" else "rien n'est mis à la corbeille par la relecture"
            raise ValueError(msg)
        kept_copy = action.get("garde")
        if kept_copy and not any(
            os.path.exists(os.path.join(roots[kept_copy["racine"]], *prefix, nfc(kept_copy["chemin"])))
            for prefix in ((), (archive_of(roots[kept_copy["racine"]]),))
        ):
            raise ValueError("l'exemplaire gardé à sa place n'est plus là")
        to_trash(src)
        prune(os.path.dirname(src), roots[action["source"]["racine"]])
        log(f"SUPPRIMÉ {name}  -> Trash — {action.get('raison', '')}")
    else:
        raise ValueError(f"action inconnue : {action['type']}")
    return "ok"


def note_download(action, name, size, dest, detail=""):
    seen = [x for x in read_json(DOWNLOADS_SEEN, []) if time.time() - x.get("t", 0) < 7 * 86400]
    when = datetime.now().isoformat(timespec="seconds")
    seen.append({
        "id": hashlib.sha1(f"{name}|{size}|{when}".encode()).hexdigest()[:16],
        "t": time.time(),
        "quand": when,
        "action": action,
        "nom": name,
        "taille": size,
        "vers": dest,
        "detail": detail,
    })
    write_json(DOWNLOADS_SEEN, seen[-300:])


def move_downloads():
    if not os.path.isdir(DOWNLOADS) or not os.path.isdir(DRIVE):
        return
    kept = load_kept()
    for name in sorted(os.listdir(DOWNLOADS)):
        path = os.path.join(DOWNLOADS, name)
        if name.startswith(".") or name.lower().endswith(PARTIAL_EXT) or not os.path.isfile(path) or sig(path) in kept:
            continue
        st = os.stat(path)
        if time.time() - max(st.st_mtime, st.st_ctime) < MIN_AGE:
            continue
        if not name.lower().endswith(DOCUMENTS):
            save_kept(path)
            log(f"LAISSÉ  {name}  (not a document) left in Downloads")
            note_download("laissé", name, st.st_size, "Téléchargements", "pas un document : installeur, archive, vidéo… gardé dans Téléchargements")
            continue
        if st.st_size > MAX_SIZE:
            save_kept(path)
            log(f"TROP GROS {name}  ({st.st_size // 2**20} MB) left in Downloads")
            notify(f"{name} ({st.st_size // 2**20} Mo) trop gros pour le Drive, laissé dans Téléchargements")
            note_download(
                "trop gros",
                name,
                st.st_size,
                "Téléchargements",
                f"{st.st_size // 2**20} Mo, au-delà de {MAX_SIZE // 2**20} Mo : gardé dans Téléchargements",
            )
            continue
        os.makedirs(DRIVE_INBOX, exist_ok=True)
        target = unique(os.path.join(DRIVE_INBOX, name))
        shutil.move(path, target)
        log(f"VERSÉ   {name}  -> Drive/00 À trier")
        note_download("versé", name, st.st_size, where(target), "téléchargement du Mac versé dans la boîte du Drive, que le serveur trie")


def apply_plan():
    """Mac mode: apply plan.json, pour ~/Downloads into Drive 00 À trier, write results.json."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    try:
        plan = read_json(PLAN, {"actions": []})
        applied = {k: v for k, v in read_json(APPLIED, {}).items() if time.time() - v[1] < 7 * 86400}
        moved, kept = [], []
        for action in plan.get("actions", []):
            if action["id"] in applied:
                continue
            try:
                status = apply_action(action, moved, kept)
            except Exception as exc:
                status = f"erreur : {type(exc).__name__} {exc}"[:200]
                log(f"ERREUR  {action['type']} {action['source']['chemin']} : {status}")
            if status:
                applied[action["id"]] = [status, time.time()]
        move_downloads()
        write_json(APPLIED, applied)
        write_json(
            RESULTS,
            {
                "rendu_le": datetime.now().isoformat(timespec="seconds"),
                "appliques": {k: v[0] for k, v in applied.items()},
                "telechargements": read_json(DOWNLOADS_SEEN, []),
            },
        )
        if moved or kept:
            msg = f"{len(moved)} rangé(s)" + (f", {len(kept)} laissé(s) dans À trier" if kept else "")
            notify(moved[0] if len(moved) == 1 and not kept else msg)
    except Exception as exc:
        log(f"ERREUR  {exc}")
        notify(f"Erreur du tri : {exc}"[:200])
    finally:
        lock.close()
    audit()


def deposit():
    """Server mode: add a 'deposer' action to plan.json for the Mac to write."""
    idx = sys.argv.index("--deposit")
    root, rel, source_file = sys.argv[idx + 1 : idx + 4]
    if root not in ("icloud", "drive") or rel.startswith("00 ") or ".." in rel.split("/"):
        sys.exit("destination refused")
    with open(source_file, encoding="utf-8") as handle:
        content = handle.read()
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan = read_json(PLAN, {"actions": [], "faites": {}})
        target = {"racine": root, "chemin": nfc(rel)}
        ident = hashlib.sha1(f"deposer|{root}|{rel}|{content}".encode()).hexdigest()[:16]
        if all(a["id"] != ident for a in plan["actions"]):
            plan["actions"].append({
                "id": ident,
                "type": "deposer",
                "source": target,
                "cible": target,
                "contenu": content,
                "cree_le": time.time(),
            })
            write_json(PLAN, plan)
    log(f"À DÉPOSER {rel} ({len(content)} characters): the Mac will write it on its next pass")


def run_cli():
    args = sys.argv[1:]
    if "--deposit" in args:
        args = args[: args.index("--deposit") + 1] + args[args.index("--deposit") + 4 :]
    unknown = [a for a in args if a not in ("--dry-run", "--decide", "--apply", "--deposit")]
    if unknown:
        sys.exit(f"unknown argument {unknown[0]}: sort.py [--dry-run] | --decide | --apply | --deposit <icloud|drive> <path> <file>")
    {"decide": decide, "apply": apply_plan, "deposit": deposit}.get(ROLE, main)()
