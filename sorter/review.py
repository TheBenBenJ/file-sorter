"""Periodic review of already-filed theme folders (keep, archive, or trash)."""

from datetime import datetime
import hashlib
import json
import os
import re
import stat
import time

import activity

from sorter.config import (
    CLAUDE_AI,
    CLEANUP,
    CLOUD_AI,
    DECISIONS,
    DRIVE,
    DRIVE_RCLONE,
    ENGINE,
    ICLOUD,
    KEPT_FROM_TRASH,
    LOCAL_AI,
    PREVIEWS,
    REPORT,
    REVIEW,
    REVIEW_AGAIN,
    REVIEW_APPLY,
    REVIEW_BILAN,
    REVIEW_BILAN_LINE,
    REVIEW_CHARS,
    REVIEW_EXT,
    REVIEW_FILES,
    REVIEW_KINDS,
    REVIEW_LEAVE,
    REVIEW_LISTING,
    REVIEW_MIN_AGE,
    REVIEW_NO_TRASH,
    REVIEW_OWNER,
    REVIEW_PASS,
    REVIEW_PASS_TIME,
    REVIEW_PAUSE,
    REVIEW_REST,
    REVIEW_RETRIES,
    REVIEW_RULES,
    REVIEW_TRASH,
    REVIEW_VISUAL,
    RULES,
    SETTINGS,
    THEME,
    UNREAD_FORMAT,
    archive_of,
    is_archive,
    log,
    nfc,
    notify,
    read_json,
    write_json,
)
from sorter.drive import drive_move, rclone
from sorter.files import extract, make_action, place, sha
from sorter.journal import MAC_FINE, public_reason
from sorter.models import Question, ask, named, unread

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["fichiers"],
    "properties": {
        "fichiers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["fichier", "raison", "decision", "doublon_de", "confiance"],
                "properties": {
                    "fichier": {"type": "string"},
                    "raison": {"type": "string"},
                    "decision": {"type": "string", "enum": list(DECISIONS)},
                    "doublon_de": {"type": ["string", "null"]},
                    "confiance": {"type": "number"},
                },
            },
        }
    },
}

REVIEW_ITEM = {
    "raison": {"type": "string", "maxLength": 300},
    "decision": {"type": "string", "enum": list(DECISIONS)},
    "doublon_de": {"type": ["string", "null"], "maxLength": 200},
    "confiance": {"type": "number"},
}

CODE_MARKERS = {"package.json", "composer.json", "pom.xml", ".git", "angular.json", "src", "app", "vendor", "node_modules"}
COPY_WORDS = re.compile(
    r"\b(copie|doublon|double|duplicata|identique|exemplaire|version|réplique|déjà|autre fichier|même|qualité|"
    r"moins (bonne?|nette?|lisible|complète?)|plus (floue?|nette?|lisible|complète?|récente?))",
    re.I,
)
PHOTOS = "80 Images"


def review_prompt(root, folder, files, archive, blocks, local=False):
    confidence = (
        '- "confiance" : ta certitude sur cette décision, de 0 (au hasard) à 1 (certain). Si tu n\'es pas sûr, mets moins de 0.8.'
        if local
        else '- "confiance" entre 0 et 1. Si tu n\'es pas sûr, mets moins de 0.8.'
    )
    drive_label = "Google Drive" if root == "drive" else "iCloud Drive"
    owner_extra = chr(10) + REVIEW_OWNER if REVIEW_OWNER else ""
    return f"""Tu fais le ménage dans les documents personnels de {SETTINGS['proprietaire']}, rangés par thèmes dans son {drive_label}. Nous sommes le {datetime.now():%d/%m/%Y}.
Les dossiers par thèmes ne gardent que les documents officiels encore d'actualité. Le reste part dans « {archive} » (au même chemin) s'il peut encore servir de preuve, sinon à la corbeille.

Dossier relu : « {folder} ». Tous ses fichiers, pour repérer les doublons et les versions plus récentes :
{files}

Pour chaque fichier examiné plus bas, "decision" vaut :
- "garder" : SEULEMENT un document officiel encore d'actualité : pièce d'identité, passeport, permis, carte grise ou autre titre encore valable ; acte d'état civil, livret de famille ; diplôme, titre professionnel ; RIB et attestations encore valables (assurance, droits, Sécurité sociale) ; contrat signé encore en cours (bail, travail, prêt, assurance, mutuelle, abonnement) avec ses conditions particulières ; acte notarié, titre de propriété (l'acte de vente où il est acheteur), règlement de copropriété, diagnostics et plans d'un logement encore possédé ; décision ou courrier encore utile d'une administration (impôts, URSSAF, CAF, Sécurité sociale, préfecture, tribunal) ; dossier ou démarche en cours, avec ses formulaires remplis et ses pièces ; compte rendu, résultat ou ordonnance médicaux de moins de deux ans ; avis d'impôt, bulletin de paie, relevé bancaire, facture ou justificatif de l'auto-entreprise de l'année en cours ou de l'année précédente.
- "archiver" : un document officiel ou une preuve qui ne sert plus au quotidien : pièce périmée ou remplacée par une plus récente, contrat terminé, ancien logement ou ancien employeur, dossier clos, ancien document médical ; avis d'impôt, bulletin de paie, relevé, facture ou justificatif plus anciens ; facture d'achat d'un bien (preuve d'achat, garantie).
- "supprimer" (corbeille) : ce qui n'est pas un document officiel et ne sert plus : reçu de paiement, ticket de caisse, bon de retrait, confirmation de commande, de souscription, d'inscription ou de réservation, preuve de dépôt, avis de réception ou suivi d'un recommandé ou d'un colis, accusé de réception, billet ou événement passé, devis non signé, test Covid, publicité, newsletter, conditions générales (de vente, d'utilisation), notice, mentions légales, politique de confidentialité, document générique qui ne le concerne pas personnellement, formulaire vierge, capture d'écran, brouillon, code, QR code, pré-demande, convocation ou rendez-vous passés ; ou copie moins bonne d'un autre fichier examiné plus bas (le même document : scan plus flou, photo d'un document déjà scanné, version incomplète).
- Un reçu, une confirmation, un accusé ou un avis de réception, une preuve de dépôt ou un suivi n'est pas un document officiel, même quand il vient d'une administration ou concerne un contrat en cours : "supprimer".
- Un contrat signé (prêt, bail, assurance, mutuelle, abonnement, travail) et le logement qu'il concerne restent en cours tant que la liste ne montre pas leur fin (résiliation, vente où il est vendeur, état des lieux de sortie, dernier remboursement, contrat plus récent qui le remplace) : sans cette preuve, "garder". Un prêt immobilier court jusqu'à sa dernière échéance.
- Une démarche aboutie ne garde que son résultat : quand le document obtenu est dans la liste (nouvelle carte, nouveau titre, contrat signé), les pièces qui ont servi à l'obtenir ne servent plus : pré-demande, récépissé, timbre fiscal, convocation, attestation de stage ou de présence, formulaire, devis, déclaration de perte ou de vol, accusé de réception : "supprimer". Le résultat, lui, reste : le titre, la carte, et toute décision ou lettre d'une administration (retrait ou retour de points, suspension, avis) se garde ou s'archive, jamais à la corbeille. Un timbre fiscal de plus d'un an ne sert plus.
- Jamais "supprimer" une pièce d'identité, un acte d'état civil (extrait d'acte de naissance, livret de famille), un diplôme, un contrat signé, une décision d'une administration ou d'un tribunal, un bulletin de paie, un avis d'impôt, ni une facture ou un justificatif de l'auto-entreprise, sauf une copie moins bonne du même document (même pièce, même page) : archive-les plutôt. Un extrait d'acte et le livret de famille sont deux documents différents.
- Dans le doute entre "garder" et "archiver" : archiver, rien n'y est perdu.
- Plusieurs versions d'un même document : garder la meilleure (la plus récente encore valable, complète et lisible), archiver les anciennes, supprimer les copies moins bonnes. Le recto et le verso d'un même document ne sont pas des doublons.
- "doublon_de" : OBLIGATOIRE pour une copie moins bonne à supprimer : le nom exact du fichier examiné qu'on garde à sa place ; sinon null.
Règles du propriétaire (elles priment sur tout ce qui précède quand elles disent autre chose) :
{RULES}{owner_extra}
{confidence}
- "raison" : une phrase courte.
- Le champ "fichier" doit reprendre EXACTEMENT le nom donné après "###". Réponds pour CHAQUE fichier.

Fichiers à examiner :

{chr(10).join(blocks)}
"""


def theme_walk(cursor):
    """Yield theme folders of iCloud then Drive strictly after cursor, skipping code/bundles/archives."""
    bundles = tuple(SETTINGS.get("paquets") or ()) + (PHOTOS,)
    for idx, (root, base) in enumerate((("icloud", ICLOUD), ("drive", DRIVE))):
        if (cursor and idx < cursor[0]) or not os.path.isdir(base):
            continue
        for cur, dirs, files in os.walk(base):
            rel = nfc(os.path.relpath(cur, base))
            parts = () if rel == "." else tuple(rel.split(os.sep))
            here = (idx,) + parts
            if parts and CODE_MARKERS & set(dirs + files):
                dirs[:] = []
                continue
            kept = []
            for d in sorted(dirs, key=nfc):
                name = nfc(d)
                sub = parts + (name,)
                if name.startswith(".") or any(b in "/" + "/".join(sub) for b in bundles):
                    continue
                if not parts and (not THEME.match(name) or name.startswith("00 ") or is_archive(name)):
                    continue
                if cursor and (idx,) + sub < cursor and cursor[: len(sub) + 1] != (idx,) + sub:
                    continue
                kept.append(d)
            dirs[:] = kept
            if parts and not (cursor and here <= cursor):
                yield here, root, base, cur, rel, files


def exact_copies(docs):
    """Return {copy_path: kept_path} for byte-identical documents in the same folder."""
    by_size = {}
    for path in docs:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size:
            by_size.setdefault(size, []).append(path)
    rank = lambda p: (
        bool(re.search(r"\(\d+\)", os.path.basename(p))),
        not re.match(r"\d{4}-\d\d-\d\d ", os.path.basename(p)),
        len(os.path.basename(p)),
        os.path.basename(p),
    )
    out = {}
    for same_size in (group for group in by_size.values() if len(group) > 1):
        by_hash = {}
        for path in same_size:
            try:
                by_hash.setdefault(sha(path), []).append(path)
            except OSError:
                continue
        for group in (g for g in by_hash.values() if len(g) > 1):
            kept = min(group, key=rank)
            out.update({p: kept for p in group if p != kept})
    return out


def listing(docs, chunk):
    """Format up to REVIEW_LISTING folder filenames and sizes around the active chunk."""
    shown = docs
    if len(docs) > REVIEW_LISTING:
        first = min(docs.index(p) for p in chunk)
        start = max(0, min(first - REVIEW_LISTING // 4, len(docs) - REVIEW_LISTING))
        shown = docs[start : start + REVIEW_LISTING]
        shown += [p for p in chunk if p not in shown]
    lines = [f"- {os.path.basename(p)} ({max(1, round(os.path.getsize(p) / 1024))} Ko)" for p in shown]
    if len(shown) < len(docs):
        lines.append(f"- … et {len(docs) - len(shown)} autre(s) fichier(s)")
    return "\n".join(lines)


def judge(decided, groups, copies):
    """Validate review decisions with confidence thresholds and copy safeguards."""
    together = {p: grp for grp in groups for p in grp}
    order = sorted(decided, key=lambda p: nfc(os.path.basename(p)).lower())
    out = {}
    for path in order:
        ans, ai = decided[path]
        decision = wanted = ans.get("decision") if ans.get("decision") in DECISIONS else "garder"
        raw_conf = ans.get("confiance")
        conf = float(raw_conf) if type(raw_conf) in (int, float) and 0 <= raw_conf <= 1 else 0.0
        reason = str(ans.get("raison") or "").strip().rstrip(".")
        kept, proposed = None, None
        if decision != "garder" and conf < REVIEW_LEAVE:
            decision, proposed = "garder", f"{decision}, pas assez sûr"
        others = [x for x in together.get(path, ()) if x != path]
        cited = []
        if decision == "supprimer" and ans.get("doublon_de"):
            kept = named(ans["doublon_de"], others)
            if not kept:
                decision, proposed = "garder", "supprimer comme copie d'un fichier qu'il n'a pas vu"
        elif decision == "supprimer":
            cited = sorted(
                (x for x in others if nfc(os.path.basename(x)).lower() in nfc(reason).lower()),
                key=lambda x: -len(os.path.basename(x)),
            )
            if COPY_WORDS.search(reason):
                kept = (
                    cited[0]
                    if len(cited) == 1 or (cited and len(os.path.basename(cited[0])) > len(os.path.basename(cited[1])))
                    else None
                )
                if not kept:
                    decision, proposed = "garder", "supprimer comme copie, sans nommer le fichier gardé"
        out[path] = [decision, conf, kept, reason, ai, proposed, wanted, cited]

    for path in order:
        verdict = out[path]
        refs = [verdict[2]] if verdict[2] else verdict[7]
        if verdict[0] == "supprimer" and any(out.get(x, ["garder"])[0] == "supprimer" for x in refs):
            verdict[0], verdict[2], verdict[5] = "garder", None, "supprimer, mais l'autre exemplaire part aussi"

    for verdict in out.values():
        if verdict[0] == "supprimer" and verdict[1] < REVIEW_TRASH:
            verdict[0], verdict[5] = "archiver", "supprimer, pas assez sûr pour la corbeille"

    for path, kept in copies.items():
        out[path] = ["supprimer", 1.0, kept, f"copie identique de « {os.path.basename(kept)} »", None, None, None]
    return {p: tuple(v[:7]) for p, v in out.items()}


class Review:
    """State machine for reviewing theme folders within a --decide pass."""

    def __init__(self, actions, later):
        self.now = time.time()
        state, broken = read_json(REVIEW, {}), False
        if not isinstance(state, dict) or not isinstance(state.get("files", {}), dict):
            log("review: review.json unreadable, the review starts again from the first folder")
            state, broken = {}, True
        state["files"] = {
            k: e
            for k, e in (state.get("files") or {}).items()
            if isinstance(e, dict) and isinstance(e.get("path"), str) and k.count("|") >= 2
        }
        cursor = state.get("cursor")
        if not (isinstance(cursor, list) and cursor and type(cursor[0]) is int and all(isinstance(x, str) for x in cursor[1:])):
            state["cursor"] = None
        state["withdrawn"] = {
            k: w
            for k, w in (state.get("withdrawn") or {}).items()
            if isinstance(w, dict) and isinstance(w.get("action"), dict) and w["action"].get("id") == k
        }
        before = state.get("rules", 1)
        migrated = before < REVIEW_RULES
        if migrated:
            loc = lambda k, e: (k.split("|", 1)[0], nfc(os.path.dirname(e["path"])))
            again = set()
            if before < 2:
                again = {
                    loc(k, e)
                    for k, e in state["files"].items()
                    if e.get("decision") == "garder"
                    and e.get("state") == "kept"
                    and e.get("engine")
                    and type(e.get("confidence")) in (int, float)
                    and REVIEW_LEAVE <= e["confidence"] < REVIEW_TRASH
                }
            due = {k for k, e in state["files"].items() if loc(k, e) in again}
            if before < 3:
                due |= {k for k, e in state["files"].items() if e.get("state") == "kept" and e.get("engine")}
            if before < 4:
                due |= {k for k, e in state["files"].items() if e.get("engine") and e.get("state") in ("kept", "pending")}
            if before < 6:
                buying = re.compile(
                    r"acte de vente|promesse|compromis|offre d'achat|lettre d'intention|financement|prêt immobilier|copropriété|diagnostic",
                    re.I,
                )
                places = {loc(k, e) for k, e in state["files"].items() if buying.search(nfc(os.path.basename(e["path"])))}
                due |= {
                    k
                    for k, e in state["files"].items()
                    if loc(k, e) in places and e.get("engine") and e.get("state") in ("kept", "pending")
                }
            if before < 7:
                due |= {
                    k
                    for k, e in state["files"].items()
                    if e.get("reason") == UNREAD_FORMAT and not e.get("engine") and e.get("state") == "kept"
                }
            for key in due:
                state["files"][key]["time"] = 0
            if due:
                state["cursor"] = None
                state.pop("rest_until", None)
                state["bilan_at_end"] = True
                log(f"review: new rules, {len({loc(k, state['files'][k]) for k in due})} folder(s) read again ({len(due)} document(s))")
            state["rules"] = REVIEW_RULES

        owner_hash = hashlib.sha1((RULES + "\n" + REVIEW_OWNER).encode("utf-8")).hexdigest()[:12]
        if state.get("owner") != owner_hash:
            if "owner" in state or REVIEW_OWNER:
                loc = lambda k, e: (k.split("|", 1)[0], nfc(os.path.dirname(e["path"])))
                again = {k for k, e in state["files"].items() if e.get("engine") and e.get("state") in ("kept", "pending")}
                for key in again:
                    state["files"][key]["time"] = 0
                if again:
                    state["cursor"] = None
                    state.pop("rest_until", None)
                    state["bilan_at_end"] = True
                    log(f"review: the owner's rules changed, {len({loc(k, state['files'][k]) for k in again})} folder(s) read again ({len(again)} document(s))")
            state["owner"], migrated = owner_hash, True

        spared = [
            (k.split("|", 1)[0], e)
            for k, e in state["files"].items()
            if k.split("|", 1)[0] in REVIEW_NO_TRASH and e.get("decision") == "supprimer" and e.get("state") == "pending"
        ]
        for root in REVIEW_NO_TRASH:
            mine = [e for r, e in spared if r == root]
            for entry in mine:
                entry.update(decision="archiver", proposal="supprimer")
            if mine:
                log(f"review: {len(mine)} decision(s) to trash in {place(root)} go into its archive instead: nothing is trashed there")
                later.append((
                    "revu",
                    place(root),
                    {"detail": f"{len(mine)} document(s) proposé(s) pour la corbeille partent plutôt aux archives : {KEPT_FROM_TRASH(root)}"},
                ))
        self.state = state
        self.files = state["files"]
        self.withdrawn = state["withdrawn"]
        self.read_as = None if broken or migrated or spared else json.dumps(state, sort_keys=True)
        self.actions = actions
        self.later = later
        self.planned = set()
        self.by_server = None
        self.roots = {}
        self.index = None
        self.asked = 0

    def changed(self):
        return json.dumps(self.state, sort_keys=True) != self.read_as

    def save(self):
        write_json(REVIEW, self.state)

    def returned(self, action, status):
        key = f"{action['source']['racine']}|{nfc(action['source']['chemin'])}|{action['taille']}"
        if key not in self.files:
            return
        if status == "ok":
            self.files[key].update(state="done", time=self.now)
        elif status in MAC_FINE:
            del self.files[key]
        else:
            self.files[key].update(state="failed", time=self.now)

    def due(self, key):
        entry = self.files.get(key)
        if not entry:
            return True
        if entry.get("state") == "unanswered":
            wait = REVIEW_RETRIES[min(max(entry.get("tries", 1), 1), len(REVIEW_RETRIES)) - 1]
        else:
            wait = REVIEW_AGAIN
        return self.now - entry.get("time", 0) >= wait

    def why(self, entry, root=None):
        reason = entry.get("reason") or ""
        if entry.get("twin") and not reason.startswith("copie identique"):
            reason = f"copie moins bonne de « {os.path.basename(entry['twin'])} »" + (f" : {reason}" if reason else "")
        if entry.get("unread"):
            reason = (reason or "décision du modèle") + " ; décidé sur son nom, son contenu ne se lit pas"
        if entry.get("decision") == "archiver" and entry.get("proposal") == "supprimer":
            suffix = (
                f" ; à l'archive plutôt qu'à la corbeille : {KEPT_FROM_TRASH(root)}"
                if root in REVIEW_NO_TRASH
                else " ; à l'archive plutôt qu'à la corbeille" + ("" if entry.get("unread") else ", faute de certitude")
            )
            reason = (reason or "décision du modèle") + suffix
        return reason or "décision du modèle"

    def mounted(self, root):
        if root not in self.roots:
            try:
                base = DRIVE if root == "drive" else ICLOUD
                self.roots[root] = any(THEME.match(nfc(d)) and not nfc(d).startswith("00 ") for d in os.listdir(base))
            except OSError:
                self.roots[root] = False
        return self.roots[root]

    def apply_pending(self):
        waiting = sorted(
            ((k, e) for k, e in self.files.items() if e.get("state") == "pending" and self.mounted(k.split("|", 1)[0])),
            key=lambda x: (x[1]["decision"] != "archiver", x[1].get("time", 0)),
        )
        done = 0
        for key, entry in waiting:
            if done >= REVIEW_APPLY:
                break
            done += self.apply(key, entry)

    def apply(self, key, entry):
        root = key.split("|", 1)[0]
        base = DRIVE if root == "drive" else ICLOUD
        path, rel = os.path.join(base, entry["path"]), nfc(entry["path"])
        if root in REVIEW_NO_TRASH and entry["decision"] == "supprimer":
            entry.update(decision="archiver", proposal="supprimer")
        if f"{root}|{rel}" in self.planned:
            return False
        if not os.path.isfile(path) or str(os.path.getsize(path)) != key.rsplit("|", 1)[1]:
            del self.files[key]
            return False
        archive, name, folder = archive_of(base), os.path.basename(path), os.path.dirname(entry["path"])
        if entry["decision"] == "supprimer" and entry.get("twin") and not any(
            os.path.exists(os.path.join(base, *prefix, entry["twin"])) for prefix in ((), (archive,))
        ):
            entry.update(state="failed", time=self.now)
            log(f"ERREUR  {name} not trashed: the copy kept in its place is gone ({entry['twin']})")
            return False
        why = self.why(entry, root)
        ai = {k: entry[k] for k in ("engine", "model", "confidence") if entry.get(k) is not None}
        origin = place(root, nfc(folder))
        if root == "drive" and DRIVE_RCLONE:
            if entry["decision"] == "archiver":
                target = self.by_server(path, "archivage", lambda: drive_move(path, os.path.join(archive, folder), name))
                renamed = target and nfc(os.path.basename(target)) != nfc(name)
                event = (
                    "archivé",
                    {
                        "dst": place("drive", nfc(os.path.dirname(target or ""))),
                        "detail": f"{why} ; archivé par le serveur dans le Drive"
                        + (f", sous le nom « {os.path.basename(target)} » : le sien était pris" if renamed else ""),
                    },
                )
            else:
                target = self.by_server(
                    path,
                    "suppression",
                    lambda: rclone("deletefile", "--drive-use-trash", DRIVE_RCLONE + os.path.relpath(path, DRIVE)) or "corbeille du Drive",
                )
                event = ("supprimé", {"dst": "corbeille du Drive", "detail": f"{why} ; mis à la corbeille du Drive par le serveur"})
            if not target:
                entry["tries"] = entry.get("tries", 0) + 1
                if entry["tries"] >= 3:
                    entry.update(state="failed", time=self.now)
                return True
            entry.update(state="done", time=self.now)
            activity.log(event[0], name, src=origin, **event[1], **ai)
            verb = "ARCHIVÉ" if entry["decision"] == "archiver" else "SUPPRIMÉ"
            log(f"{verb:8} {name}  ->  {event[1]['dst']} (by the server) — {why}")
            return True
        if entry["decision"] == "archiver":
            target = {"racine": root, "chemin": nfc(os.path.join(archive, entry["path"]))}
            self.actions.append(make_action("archiver", path, cible=target, raison=why))
            self.later.append((
                "archivé",
                name,
                {"src": origin, "dst": place(root, os.path.dirname(target["chemin"])), "detail": f"{why} ; le Mac l'archive à son prochain passage", **ai},
            ))
        else:
            extra = {"garde": {"racine": root, "chemin": nfc(entry["twin"])}} if entry.get("twin") else {}
            self.actions.append(make_action("supprimer", path, raison=why, **extra))
            self.later.append((
                "supprimé",
                name,
                {"src": origin, "dst": "Corbeille du Mac", "detail": f"{why} ; le Mac le met à la Corbeille à son prochain passage", **ai},
            ))
        self.planned.add(f"{root}|{rel}")
        log(f"PLAN    {name}  ->  {entry['decision']} — {why}")
        return True

    def walk(self):
        while self.step() and self.asked < REVIEW_PASS and time.time() - self.now < REVIEW_PASS_TIME:
            pass

    def step(self):
        if self.now < max(self.state.get("rest_until", 0), self.state.get("pause_until", 0)):
            return False
        cursor = tuple(self.state.get("cursor") or ())
        for here, root, base, cur, rel, names in theme_walk(cursor):
            found = self.folder(root, base, cur, rel, names)
            if found:
                return self.read(here, root, base, cur, rel, *found)
            self.state["cursor"] = list(here)
        self.walk_over()
        return False

    def folder(self, root, base, cur, rel, names):
        if self.index is None:
            self.index = {}
            for k, entry in self.files.items():
                self.index.setdefault((k.split("|", 1)[0], nfc(os.path.dirname(entry.get("path") or ""))), set()).add(k)
        keys, listed, docs = {}, [], []
        for name in names:
            path = os.path.join(cur, name)
            if name.startswith(".") or not name.lower().endswith(REVIEW_EXT):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            keys[path] = key = f"{root}|{nfc(os.path.relpath(path, base))}|{st.st_size}"
            entry = self.files.get(key) or {}
            leaving = entry.get("state") == "pending" and entry.get("decision") == "archiver" and not self.due(key)
            if entry.get("state") == "done" or leaving or f"{root}|{nfc(os.path.relpath(path, base))}" in self.planned:
                continue
            listed.append(path)
            if self.now - st.st_mtime >= REVIEW_MIN_AGE:
                docs.append(path)
        for stale_key in self.index.get((root, rel), set()) - set(keys.values()):
            self.files.pop(stale_key, None)
        order = lambda p: nfc(os.path.basename(p)).lower()
        listed.sort(key=order)
        docs.sort(key=order)
        todo = [p for p in docs if self.due(keys[p])]
        if not todo:
            return None
        alone = all(
            (self.files.get(keys[p]) or {}).get("state") == "unanswered" and self.files[keys[p]].get("tries", 1) >= 2
            for p in todo
        )
        chunk = todo[:1] if alone else docs if len(docs) <= REVIEW_FILES else todo[:REVIEW_FILES]
        return listed, docs, keys, chunk, set(todo) <= set(chunk)

    def read(self, here, root, base, cur, rel, listed, docs, keys, chunk, complete):
        self.asked += len(chunk)
        copies = {p: k for p, k in exact_copies(docs).items() if p in chunk}
        asked = [p for p in chunk if p not in copies]
        archive = archive_of(base)
        text = listing(listed, chunk)
        question = Question(
            lambda blocks, local=False: review_prompt(root, rel, text, archive, blocks, local),
            REVIEW_SCHEMA,
            REVIEW_ITEM,
            (cur, PREVIEWS),
            " (la première page suffit)",
        )
        REPORT.running("decide")
        items = [(p, (lambda t: t[:REVIEW_CHARS] if t else None)(extract(p))) for p in asked]
        unread_docs = {p for p, t in items if not t and not p.lower().endswith(REVIEW_VISUAL)}
        items = [(p, unread(p) if p in unread_docs else t) for p, t in items]
        _, to_claude, local, missing, claude, failure, groups = (
            ask(items, question, False, "review: ") if items else ([], [], {}, {}, {}, None, [])
        )
        verdicts = judge(
            {**{p: (d, LOCAL_AI) for p, d in local.items()}, **{p: (d, CLOUD_AI) for p, d in claude.items()}},
            groups,
            copies,
        )
        verdicts = {
            p: ("archiver", *v[1:5], "supprimer, sur un nom seulement", v[6] or "supprimer")
            if v[0] == "supprimer" and p not in copies and (p in unread_docs or v[2] in unread_docs)
            else v
            for p, v in verdicts.items()
        }
        if root in REVIEW_NO_TRASH:
            verdicts = {
                p: ("archiver", *v[1:5], f"supprimer, jamais de corbeille{' dans le Drive' if root == 'drive' else ''}", v[6] or "supprimer")
                if v[0] == "supprimer"
                else v
                for p, v in verdicts.items()
            }
        to_claude_set = {p for p, _ in to_claude}
        unanswered = [p for p in asked if p not in verdicts]
        out_of_reach = failure is not None or any(missing.get(p, "").startswith("injoignable") for p in unanswered)
        retried = []
        input_error = failure is not None and re.search(r"invalid_request|could not process|too (long|large)|exceed", str(failure), re.I)
        for path in unanswered:
            prev = self.files.get(keys[path]) or {}
            if prev and prev.get("state") != "unanswered" and not self.due(keys[path]):
                continue
            retried.append(path)
            provider_label = "Antigravity" if ENGINE == "antigravity" else "Claude"
            why = missing.get(path) or (f"{provider_label} : {public_reason(failure, 'decide')}" if failure and path in to_claude_set else "absent de sa réponse")
            own = not (missing.get(path, "").startswith("injoignable") or (failure is not None and path in to_claude_set and not input_error))
            self.files[keys[path]] = {
                "path": os.path.relpath(path, base),
                "state": "unanswered",
                "time": self.now,
                "reason": why,
                "tries": (prev.get("tries", 0) if prev.get("state") == "unanswered" else 0) + own,
            }
            log(f"SANS RÉPONSE {os.path.basename(path)}  (review: {why}; try {self.files[keys[path]]['tries']})")

        trial, counts, start = CLEANUP != "on", {d: 0 for d in DECISIONS}, len(self.later)
        for path, (decision, conf, kept, reason, ai, proposed, wanted) in verdicts.items():
            counts[decision] += 1
            prev = self.files.get(keys[path]) or {}
            entry = self.files[keys[path]] = {
                "path": os.path.relpath(path, base),
                "decision": decision,
                "confidence": round(conf, 3),
                "reason": reason,
                "twin": kept and os.path.relpath(kept, base),
                **(ai or {}),
                **({"proposal": wanted} if wanted else {}),
                **({"unread": True} if path in unread_docs else {}),
                "time": self.now,
                "state": "kept" if decision == "garder" else "pending",
            }
            log(f"RELU    {os.path.basename(path)}  {decision} ({conf:.2f}{', ' + proposed if proposed else ''}) — {self.why(entry, root)}")
            if decision == "garder":
                if prev.get("state") == "pending":
                    self.later.append((
                        "gardé",
                        os.path.basename(path),
                        {
                            "src": place(root, rel),
                            "detail": f"relu, reste finalement dans son dossier : {self.why(entry, root)}",
                            **({**ai, "confidence": conf} if ai else {}),
                        },
                    ))
                continue
            if not trial:
                self.apply(keys[path], entry)
                continue
            if prev.get("state") == "pending" and (prev.get("decision"), prev.get("twin")) == (decision, entry["twin"]):
                continue
            fields = {
                "src": place(root, rel),
                "detail": f"essai, rien n'a bougé : {self.why(entry, root)}",
                **({**ai, "confidence": conf} if ai else {}),
            }
            if decision == "archiver":
                self.later.append(("à archiver", os.path.basename(path), {**fields, "dst": place(root, nfc(os.path.join(archive, rel)))}))
            else:
                self.later.append(("à supprimer", os.path.basename(path), {**fields, "dst": "corbeille du Drive" if root == "drive" else "Corbeille du Mac"}))

        if verdicts:
            words = ("à archiver", "à supprimer") if trial else ("archivé(s)", "supprimé(s)")
            summary = (
                f"{len(verdicts)} document(s) relu(s) : {counts['garder']} gardé(s), {counts['archiver']} {words[0]}, {counts['supprimer']} {words[1]}"
                + (f", {len(unanswered)} sans réponse" if unanswered else "")
                + (" ; essai : rien n'a bougé" if trial else "")
            )
            self.later.insert(start, ("revu", place(root, rel), {"detail": summary}))
            log(f"REVU    {place(root, rel)} : {summary}")
            sweep = self.state.setdefault("sweep", {})
            for key_name, count in (("files", len(verdicts)), ("archiver", counts["archiver"]), ("supprimer", counts["supprimer"])):
                sweep[key_name] = sweep.get(key_name, 0) + count

        if out_of_reach:
            self.state["pause_until"] = self.now + REVIEW_PAUSE
            why = public_reason(failure, "decide") if failure else "modèle local injoignable"
            REPORT.set("decide", "failed", f"relecture des dossiers en pause une heure : {why}")
            log(f"review: {len(unanswered)} document(s) without an answer ({why}): the review waits an hour")
        else:
            REPORT.set("decide", "ok")
        if complete and not (out_of_reach and any(self.files[keys[p]]["tries"] < len(REVIEW_RETRIES) for p in retried)):
            self.state["cursor"] = list(here)
        return True

    def walk_over(self):
        sweep = self.state.pop("sweep", None) or {}
        self.state.update(cursor=None, rest_until=self.now + REVIEW_REST)
        after_rules = self.state.pop("bilan_at_end", None)
        for key in [
            k
            for k, e in self.files.items()
            if self.mounted(k.split("|", 1)[0])
            and not os.path.exists(os.path.join(DRIVE if k.startswith("drive|") else ICLOUD, e.get("path") or "?"))
        ]:
            del self.files[key]
        if sweep.get("files"):
            log(f"REVU    every theme folder read: {sweep['files']} document(s), {sweep.get('archiver', 0)} to archive, {sweep.get('supprimer', 0)} to trash")
            if CLEANUP != "on" or after_rules:
                self.state.pop("bilan", None)
        if sweep.get("files") and not self.state.get("first_sweep"):
            self.state["first_sweep"] = self.now
            if CLEANUP == "on":
                n, a, s = sweep["files"], sweep.get("archiver", 0), sweep.get("supprimer", 0)
                notify(f"Relecture des dossiers terminée : {n} documents relus, {a} archivés, {s} mis à la corbeille")
            else:
                decided = [e for e in self.files.values() if e.get("decision")]
                a, s = (sum(e["decision"] == d and e.get("state") == "pending" for e in decided) for d in ("archiver", "supprimer"))
                notify(
                    f"Relecture d'essai des dossiers terminée : {len(decided)} documents relus, {a} à archiver et {s} à supprimer proposés, "
                    "rien n'a bougé. Le détail est dans le journal d'activité"
                )

    def bilan(self):
        if self.state.get("bilan") == REVIEW_BILAN:
            return
        roots = [r for r in ("icloud", "drive") if self.mounted(r)]
        if len(roots) < 2:
            return
        out, line, used, totals, count, on_name = [], [], 0, {}, 0, 0
        for _, root, base, cur, rel, names in theme_walk(None):
            piece = None
            for name in sorted(names, key=lambda n: nfc(n).lower()):
                path = os.path.join(cur, name)
                if name.startswith(".") or not name.lower().endswith(REVIEW_EXT):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                entry = self.files.get(f"{root}|{nfc(os.path.relpath(path, base))}|{st.st_size}") or {}
                state, decision, conf = entry.get("state"), entry.get("decision"), entry.get("confidence")
                if state == "done":
                    continue
                if not entry:
                    row = [nfc(name), "n"]
                elif state == "unanswered":
                    row = [nfc(name), "u"]
                elif state in ("pending", "failed") and decision in REVIEW_KINDS:
                    if root in REVIEW_NO_TRASH and decision == "supprimer":
                        entry = {**entry, "decision": "archiver", "proposal": "supprimer"}
                    code = "x" if state == "failed" else "a" if entry["decision"] == "archiver" else "s"
                    row = [nfc(name), code, conf, self.why(entry, root)]
                elif entry.get("reason") == UNREAD_FORMAT and not entry.get("engine"):
                    row = [nfc(name), "f"]
                else:
                    wanted = entry.get("proposal")
                    row = [nfc(name), "g", conf] + ([wanted, entry.get("reason") or ""] if wanted in REVIEW_KINDS else [])
                totals[row[1]] = totals.get(row[1], 0) + 1
                count += 1
                on_name += bool(entry.get("unread")) and row[1] in ("g", "a", "s", "x")
                size = len(json.dumps(row, ensure_ascii=False)) + 1
                if piece is None or used + size > REVIEW_BILAN_LINE:
                    head = len(json.dumps({"r": root, "f": rel, "d": []}, ensure_ascii=False)) + 1
                    if line and used + head + size > REVIEW_BILAN_LINE:
                        out.append(line)
                        line, used = [], 0
                    piece = {"r": root, "f": rel, "d": []}
                    line.append(piece)
                    used += head
                piece["d"].append(row)
                used += size
        if line:
            out.append(line)
        for idx, part in enumerate(out, 1):
            log(f"BILAN   {idx}/{len(out)} {json.dumps(part, ensure_ascii=False, separators=(',', ':'))}")
        words = {"g": "kept", "a": "to archive", "s": "to trash", "x": "failed", "f": "unreadable", "u": "without an answer", "n": "not read yet"}
        log(
            f"BILAN   end: {count} document(s), "
            + ", ".join(f"{totals[k]} {w}" for k, w in words.items() if totals.get(k))
            + (f"; {on_name} decided on their name" if on_name else "")
            + f" (FILE_SORTER_CLEANUP={CLEANUP})"
        )
        self.state["bilan"] = REVIEW_BILAN
