"""Activity journal event formatters and public French error messages for Dashboard reporting."""

import os
import re
import subprocess
import time

from sorter.config import DATA, DRIVE_INBOX, REVIEW_KINDS, THRESHOLD, nfc, read_json, write_json
from sorter.files import place, twin, where
from sorter.models import usage_limit

MAC_DONE = {
    "ranger": "rangé par le Mac",
    "doublon": "doublon jeté par le Mac",
    "vers_a_trier": "mis à trier par le Mac",
    "deposer": "déposé par le Mac",
    "archiver": "archivé par le Mac",
    "supprimer": "supprimé par le Mac",
}
MAC_VERB = {
    "ranger": "le ranger",
    "doublon": "le mettre à la Corbeille",
    "vers_a_trier": "le mettre dans 00 À trier",
    "deposer": "l'écrire",
    "archiver": "l'archiver",
    "supprimer": "le mettre à la Corbeille",
}
MAC_FINE = ("ok", "absent", "modifié depuis la décision")
DRIVE_WHAT = {
    "doublon": "mise à la corbeille du Drive",
    "vers 00 À trier": "déplacement vers 00 À trier",
    "rangement": "rangement dans le Drive",
    "archivage": "archivage dans le Drive",
    "suppression": "mise à la corbeille du Drive",
}
DOWNLOADS_LOGGED = os.path.join(DATA, "downloads-logged.json")


def kept_why(conf, answer):
    """Format the reason a file stays where it is for activity.jsonl."""
    if answer.get("_copy"):
        why = "pièce jointe sans nouveau nom proposé : sans doute la copie d'un autre document"
    elif conf < THRESHOLD:
        why = f"pas assez sûr ({round(conf * 100)} %, il faut {round(THRESHOLD * 100)} %)"
    else:
        why = "dossier proposé hors de l'arborescence"
    proposed = answer.get("destination")
    reason = str(answer.get("raison") or "").strip().rstrip(".")
    return f"{why} ; proposait « {proposed} »" + (f" : {reason}" if reason else "") if proposed else why


def filed(name, origin, target, answer, ai, how):
    """Build 'rangé' (and optional 'renommé') activity events for a filed document."""
    reason = str(answer.get("raison") or "").strip().rstrip(".")
    events = [
        ("rangé", name, {"src": origin, "dst": target, "detail": " ; ".join(x for x in (reason, how) if x), **ai})
    ]
    new_name = target.rsplit("/", 1)[-1]
    if nfc(new_name) != nfc(name):
        events.append(
            ("renommé", name, {"src": name, "dst": new_name, "detail": "nom refait selon la convention", **ai})
        )
    return events


def downloads_events(entries):
    """Convert Mac Downloads entries from results.json into one-time activity events."""
    logged = {k: v for k, v in read_json(DOWNLOADS_LOGGED, {}).items() if time.time() - v < 8 * 86400}
    events = []
    for entry in entries:
        try:
            if entry["id"] in logged:
                continue
            logged[entry["id"]] = time.time()
            events.append(
                (
                    entry["action"],
                    entry["nom"],
                    {
                        "src": "Mac · Téléchargements",
                        "dst": entry.get("vers") or "Téléchargements",
                        "detail": entry.get("detail") or "",
                    },
                )
            )
        except KeyError, TypeError:
            continue
    if events:
        write_json(DOWNLOADS_LOGGED, logged)
    return events


def mac_event(action, status):
    """Convert a plan action result returned by the Mac into an activity event tuple."""
    try:
        return _mac_event(action, str(status))
    except KeyError, TypeError, ValueError, AttributeError, OSError:
        return "fait par le Mac", str(action.get("id", "?")), {"detail": str(status)}


def _mac_event(action, status):
    kind = action.get("type")
    src = action.get("source") or {}
    target = action.get("cible")
    if kind == "deposer":
        obj = os.path.basename(target["chemin"])
        fields = {"src": "serveur", "dst": place(target["racine"], target["chemin"])}
    else:
        obj = os.path.basename(src.get("chemin", ""))
        dst = (
            place(target["racine"], os.path.dirname(target["chemin"]) if kind == "archiver" else target["chemin"])
            if target
            else "Corbeille du Mac"
            if kind in ("doublon", "supprimer")
            else where(DRIVE_INBOX)
        )
        fields = {"src": place(src.get("racine"), os.path.dirname(src.get("chemin", ""))), "dst": dst}
    if status == "ok":
        detail = (
            f"identique à {twin(action['original'])}"
            if action.get("original")
            else action.get("raison")
            if kind in REVIEW_KINDS
            else None
        )
        return MAC_DONE.get(kind, "fait par le Mac"), obj, {**fields, "detail": detail}
    if status in MAC_FINE:
        detail = (
            "le Mac ne l'a plus trouvé : déplacé ou supprimé entre-temps"
            if status == "absent"
            else "modifié depuis la décision : il sera examiné de nouveau"
        )
        return "annulé", obj, {**fields, "detail": detail}
    error = status.removeprefix("erreur : ")
    return (
        "échec",
        obj,
        {**fields, "detail": f"le Mac n'a pas pu {MAC_VERB.get(kind, 'appliquer la décision')} : {error}"},
    )


def public_reason(exc, step):
    """Format a failure for run-status.json (French, no private filenames)."""
    try:
        return _public_reason(exc, step)
    except KeyError, IndexError, TypeError, ValueError, AttributeError, OSError:
        return f"erreur {type(exc).__name__}"


def _public_reason(exc, step):
    if isinstance(exc, subprocess.TimeoutExpired):
        program = os.path.basename(str(exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) else exc.cmd))
        if exc.timeout >= 120:
            return f"{program} n'a pas répondu en {int(exc.timeout // 60)} min"
        return f"{program} n'a pas répondu en {int(exc.timeout)} s"
    limit = usage_limit(str(exc))
    if limit:
        return limit
    found = re.match(r"(claude|antigravity) failed \((-?\d+)\)(?:: ([a-z_]+) : )?", str(exc))
    if found:
        label = "Antigravity" if found.group(1) == "antigravity" else "Claude"
        suffix = ", " + found.group(3) if found.group(3) else ""
        return f"{label} a échoué (code {found.group(2)}{suffix})"
    if step == "decide" and isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return "réponse du modèle illisible"
    if isinstance(exc, OSError):
        return f"erreur système : {exc.strerror or type(exc).__name__}"
    return f"erreur {type(exc).__name__}"
