"""LLM prompts, strict JSON schemas, local/Claude routing, shadow comparison, and validation."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.request

from sorter import activity
from sorter.config import (
    AGY,
    AGY_MODEL,
    ATTACH_INBOX,
    CHROME_INBOXES,
    CLAUDE,
    CLOUD_AI,
    CONVENTION,
    DOWNLOADS,
    DRIVE_INBOX,
    ENGINE,
    GMAIL_INBOX,
    INBOX,
    LOCAL_AI,
    LOCAL_ANSWER,
    LOCAL_CONTEXT,
    LOCAL_FILES,
    LOCAL_MARGIN,
    LOCAL_MODEL,
    LOCAL_TIMEOUT,
    LOCAL_URL,
    MODEL,
    PREVIEWS,
    RULES,
    SETTINGS,
    SHADOW,
    THRESHOLD,
    folders,
    log,
    nfc,
)
from sorter.files import clean_name, extract, jpeg_preview, safe_dest, where

# JSON schema for Claude's filing answers (French keys are part of the prompt contract).
SCHEMA = {
    "type": "object",
    "properties": {
        "fichiers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fichier": {"type": "string"},
                    "destination": {"type": "string"},
                    "nouveau_nom": {"type": ["string", "null"]},
                    "confiance": {"type": "number"},
                    "raison": {"type": "string"},
                },
                "required": ["fichier", "destination", "nouveau_nom", "confiance", "raison"],
            },
        }
    },
    "required": ["fichiers"],
}

FILING_ITEM = {
    "destination": {"type": "string", "maxLength": 200},
    "nouveau_nom": {"type": ["string", "null"], "maxLength": 150},
    "confiance": {"type": "number"},
    "raison": {"type": "string", "maxLength": 300},
}


class Question:
    """Batch question definition shared between Claude and the local OpenAI-compatible model."""

    def __init__(self, prompt_fn, schema, item, dirs, read_hint=""):
        self.prompt = prompt_fn
        self.schema = schema
        self.item = item
        self.dirs = dirs
        self.read_hint = read_hint


class Unread(str):
    """Sentinel text passed to the local model when a document's format cannot be read directly."""


def unread(path):
    ext = os.path.splitext(path)[1].lower() or "sans extension"
    return Unread(
        f"Contenu illisible (format {ext}) : décide d'après le nom du fichier, sa date et les autres fichiers du dossier."
    )


def text_block(path, txt):
    if isinstance(txt, Unread):
        return f"### {os.path.basename(path)}\n{txt}"
    return f"### {os.path.basename(path)}\nExtrait du texte :\n{txt}"


def prompt(tree, blocks, local=False):
    confidence = (
        '- "confiance" : ta certitude sur la destination et le nom que tu proposes, de 0 (au hasard) à 1 (certain). '
        'Si tu n\'es pas sûr, mets moins de 0.7 : le fichier restera dans "00 À trier".'
        if local
        else '- "confiance" entre 0 et 1. Si tu n\'es pas sûr, mets moins de 0.7 : le fichier restera dans "00 À trier".'
    )
    return f"""Tu ranges les documents personnels de {SETTINGS["proprietaire"]} dans son iCloud Drive.

Dossiers existants (chemins relatifs, à réutiliser tels quels) :
{chr(10).join("- " + f for f in tree)}

Règles :
- "destination" = un dossier de la liste ci-dessus. Tu peux ajouter UN niveau de sous-dossier sous un dossier existant si c'est clairement utile (ex. "50 Travail/Auto-entrepreneur/Factures 2026/T4" pour une facture d'octobre 2026 ; trimestres T1=jan-mars, T2=avr-juin, T3=juil-sept, T4=oct-déc).
{RULES}
- "nouveau_nom" : TOUJOURS fourni, conforme à la convention ci-dessous (null uniquement si le nom actuel la respecte déjà exactement).

{CONVENTION}
{confidence}
- "raison" : une phrase courte.
- Le champ "fichier" doit reprendre EXACTEMENT le nom donné après "###". Réponds pour CHAQUE fichier.

Fichiers à classer :

{chr(10).join(blocks)}
"""


def filing(tree):
    """Build the inbox filing Question."""
    return Question(
        lambda blocks, local=False: prompt(tree, blocks, local),
        SCHEMA,
        FILING_ITEM,
        (INBOX, DRIVE_INBOX, DOWNLOADS, GMAIL_INBOX, ATTACH_INBOX, PREVIEWS) + CHROME_INBOXES,
    )


def claude_error(res):
    """Extract a readable error summary from a failed `claude -p` invocation."""
    try:
        data = json.loads(res.stdout)
        text = " : ".join(str(data[k]) for k in ("api_error", "result") if data.get(k))
        if text:
            return text
    except ValueError, TypeError, AttributeError:
        pass
    return (res.stderr[-500:] or res.stdout[-500:]).strip()


def usage_limit(text):
    """Format Claude or Antigravity usage-limit errors into a short French status message."""
    if re.search(r"resource_exhausted|quota exceeded|rate limit", text, re.I) and "antigravity" in text.lower():
        return "limite d'usage d'Antigravity atteinte"
    if "usage_limit_reached" not in text:
        return None
    found = re.search(r"resets (\d{1,2})(?::(\d\d))?\s*([ap]m)", text)
    hour = found and int(found.group(1)) % 12 + (12 if found.group(3) == "pm" else 0)
    suffix = f", reprise vers {hour:02d}:{found.group(2) or '00'}" if found else ""
    return "limite d'usage de Claude atteinte" + suffix


def _build_cloud_blocks(items, question):
    """Prepare text/image prompt blocks and readable staging paths for a cloud CLI."""
    blocks = []
    for path, txt in items:
        name = os.path.basename(path)
        if txt:
            blocks.append(text_block(path, txt))
        elif os.path.splitext(path)[1].lower() in (".heic", ".heif", ".tiff"):
            jpg = os.path.join(
                PREVIEWS, hashlib.sha1(path.encode()).hexdigest()[:8] + "-" + re.sub(r"\W", "_", name) + ".jpg"
            )
            jpeg_preview(path, jpg)
            blocks.append(f"### {name}\nImage : lis l'aperçu JPEG avec l'outil Read à ce chemin exact : {jpg}")
        else:
            readable = (
                path
                if os.path.dirname(path) in question.dirs
                else os.path.join(PREVIEWS, hashlib.sha1(path.encode()).hexdigest()[:8] + "-" + name)
            )
            if readable != path:
                shutil.copy(path, readable)
            blocks.append(
                f"### {name}\nPas de texte extractible : lis-le avec l'outil Read à ce chemin exact : {readable}{question.read_hint}"
            )
    return blocks


def ask_antigravity(items, question):
    """Ask Antigravity CLI (`agy -p`, plan/read-only mode) to classify or review a batch of files."""
    blocks = _build_cloud_blocks(items, question)
    dirs = [arg for d in question.dirs if os.path.isdir(d) for arg in ("--add-dir", d)]
    cmd = [
        AGY,
        "-p",
        question.prompt(blocks),
        "--model",
        AGY_MODEL,
        "--mode",
        "plan",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(question.schema),
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        *dirs,
    ]
    env = dict(os.environ, PATH=f"{os.path.dirname(AGY)}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    res = subprocess.run(
        cmd, capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir(), env=env, stdin=subprocess.DEVNULL
    )
    if res.returncode != 0:
        raise RuntimeError(f"antigravity failed ({res.returncode}): {claude_error(res)}")
    data = json.loads(res.stdout)
    parsed = data.get("structured_output") or data.get("response") or data.get("result")
    if isinstance(parsed, str):
        parsed = json.loads(re.search(r"\{.*\}", parsed, re.S).group(0))
    return parsed["fichiers"]


def ask_claude(items, question):
    """Ask Claude CLI (Read tool only) to classify or review a batch of files."""
    blocks = _build_cloud_blocks(items, question)
    dirs = [arg for d in question.dirs if os.path.isdir(d) for arg in ("--add-dir", d)]
    cmd = [
        CLAUDE,
        "-p",
        question.prompt(blocks),
        "--model",
        MODEL,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(question.schema),
        "--tools",
        "Read",
        "--allowedTools",
        "Read",
        *dirs,
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--no-session-persistence",
    ]
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir(), env=env)
    if res.returncode != 0:
        raise RuntimeError(f"claude failed ({res.returncode}): {claude_error(res)}")
    data = json.loads(res.stdout)
    parsed = data.get("structured_output") or data.get("result")
    if isinstance(parsed, str):
        parsed = json.loads(re.search(r"\{.*\}", parsed, re.S).group(0))
    return parsed["fichiers"]


def ask_cloud(items, question):
    """Dispatch cloud classification to Antigravity (default) or Claude depending on ENGINE."""
    return ask_antigravity(items, question) if ENGINE == "antigravity" else ask_claude(items, question)


def local_call(path, body, timeout=LOCAL_TIMEOUT):
    req = urllib.request.Request(
        LOCAL_URL.rstrip("/") + path,
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def local_schema(names, item=FILING_ITEM):
    """Strict JSON schema for the local model: exact item count and filename enum."""
    item_props = {"fichier": {"type": "string", "enum": names}, **item}
    return {
        "type": "object",
        "properties": {
            "fichiers": {
                "type": "array",
                "minItems": len(names),
                "maxItems": len(names),
                "items": {
                    "type": "object",
                    "properties": item_props,
                    "required": list(item_props),
                },
            }
        },
        "required": ["fichiers"],
    }


def simple(name):
    """Loose filename normalization for matching model outputs (case, accents, punctuation)."""
    return re.sub(r"[^0-9a-z]", "", unicodedata.normalize("NFKD", str(name)).lower())


def named(name, files):
    """Resolve a filename returned by a model back to its source path, or None if ambiguous."""
    exact = [p for p in files if nfc(os.path.basename(p)) == nfc(str(name))]
    loose = [p for p in files if simple(os.path.basename(p)) == simple(name)]
    return exact[0] if exact else loose[0] if len(loose) == 1 else None


def match(decisions, files):
    """Map model decision dicts to existing source paths."""
    found = {}
    for item in decisions if isinstance(decisions, list) else []:
        src = named(item.get("fichier", ""), files) if isinstance(item, dict) else None
        if src and src not in found and os.path.exists(src):
            found[src] = item
    return found


def ask_local(items, question, tag=None):
    """Query the local LLM in token-budgeted batches, returning (answers, missing, groups)."""
    if LOCAL_URL in ("", "off"):
        return {}, {p: "FILE_SORTER_LOCAL_URL=off" for p, _ in items}, []
    tag = ("shadow: " if SHADOW else "") if tag is None else tag
    texts, answers, missing, groups = dict(items), {}, {}, []
    try:
        fixed = len(local_call("/tokenize", {"content": question.prompt([], True)}, 60)["tokens"])
        sizes = {p: len(local_call("/tokenize", {"content": text_block(p, t) + "\n"}, 60)["tokens"]) for p, t in items}
    except Exception as exc:
        log(f"{tag}local model unreachable: {type(exc).__name__} {str(exc)[:150]}")
        return {}, {p: f"injoignable, {type(exc).__name__}" for p, _ in items}, []

    def room(n):
        return LOCAL_CONTEXT - LOCAL_MARGIN - LOCAL_ANSWER * n - 100

    queue, current = [], []
    for path, _ in items:
        if fixed + sizes[path] > room(1):
            missing[path] = f"trop long pour un créneau, {fixed + sizes[path]} tokens"
            continue
        if current and (
            len(current) == LOCAL_FILES or fixed + sum(sizes[x] for x in current) + sizes[path] > room(len(current) + 1)
        ):
            queue.append(current)
            current = []
        current.append(path)
    if current:
        queue.append(current)

    while queue:
        current = queue.pop(0)
        names = [os.path.basename(p) for p in current]
        body = {
            "model": "local",
            "temperature": 0,
            "max_tokens": LOCAL_ANSWER * len(current) + 100,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "user", "content": question.prompt([text_block(p, texts[p]) for p in current], True)}
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "fichiers", "schema": local_schema(names, question.item), "strict": True},
            },
        }
        try:
            resp = local_call("/v1/chat/completions", body)
            choice = resp["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("answer cut at max_tokens")
            found = match(json.loads(choice["message"]["content"])["fichiers"], current)
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and len(current) > 1:
                queue[:0] = [current[: len(current) // 2], current[len(current) // 2 :]]
                continue
            if exc.code == 400:
                missing.update({p: "refusé par le modèle local, sans doute trop long" for p in current})
                continue
            remaining = current + [x for grp in queue for x in grp]
            missing.update({p: f"injoignable, HTTP {exc.code}" for p in remaining})
            log(f"{tag}local model unavailable: HTTP {exc.code}, {len(remaining)} file(s) without its answer")
            break
        except OSError as exc:
            remaining = current + [x for grp in queue for x in grp]
            missing.update({p: f"injoignable, {type(exc).__name__}" for p in remaining})
            log(
                f"{tag}local model unavailable: {type(exc).__name__} {str(exc)[:150]}, {len(remaining)} file(s) without its answer"
            )
            break
        except Exception as exc:
            missing.update({p: f"réponse illisible, {type(exc).__name__}" for p in current})
            log(
                f"{tag}local model's answer unreadable for {len(current)} file(s): {type(exc).__name__} {str(exc)[:150]}"
            )
            continue
        answers.update(found)
        groups.append(current)
        missing.update({p: "absent de sa réponse" for p in current if p not in found})
        prompt_tokens = (resp.get("usage") or {}).get("prompt_tokens")
        log(
            f"{tag}local model {LOCAL_MODEL}: {len(found)} of {len(current)} file(s) answered, {prompt_tokens} prompt tokens"
        )
    return answers, missing, groups


def outcome(src, answer, tree):
    """Validate a model filing answer: (src, dest_or_None, new_name, confidence, answer)."""
    name = os.path.basename(src)
    dest = safe_dest(str(answer.get("destination") or ""), tree)
    raw_conf = answer.get("confiance")
    conf = float(raw_conf) if type(raw_conf) in (int, float) and 0 <= raw_conf <= 1 else 0.0
    if not dest or conf < THRESHOLD:
        return src, None, name, conf, answer
    new = name
    keeps_name = dest.startswith("80 Images/Photos") or dest.startswith("80 Images/Captures")
    if answer.get("nouveau_nom") and not keeps_name:
        new = clean_name(str(answer["nouveau_nom"]), name)
    if new == name and not keeps_name and os.path.dirname(src) == ATTACH_INBOX:
        return src, None, name, conf, {**answer, "_copy": True}
    return src, dest, new, conf, answer


def described(res):
    _, dest, new, conf, answer = res
    return f"{dest}/{new} ({conf:.2f})" if dest else f"stays ({conf:.2f}, proposed « {answer.get('destination')} »)"


def compare(texts, claude, local, missing, tree):
    """Log shadow-mode disagreements between Claude and the local model."""
    same = differ = none = 0
    for path, _ in texts:
        if path not in claude:
            continue
        name, ref = os.path.basename(path), outcome(path, claude[path], tree)
        if path not in local:
            none += 1
            log(f"shadow: {name}  {MODEL}={described(ref)}  local=no answer ({missing.get(path, '?')})")
            continue
        loc = outcome(path, local[path], tree)
        if (ref[1], nfc(ref[2])) == (loc[1], nfc(loc[2])):
            same += 1
        else:
            differ += 1
            log(f"shadow: {name}  {MODEL}={described(ref)}  local={described(loc)}")
    if same + differ + none:
        log(
            f"shadow: {same + differ + none} file(s) with text compared: {same} the same, {differ} different, {none} without a local answer"
        )


def ask(items, question, shadow, tag=None):
    """Route text items to the local model and visual items (or all in shadow mode) to the cloud engine."""
    texts = [(p, t) for p, t in items if t]
    to_claude = items if shadow else [(p, t) for p, t in items if not t]
    local, missing, groups = ask_local(texts, question, tag) if texts else ({}, {}, [])
    claude, failure = {}, None
    if to_claude:
        try:
            claude = match(ask_cloud(to_claude, question), [p for p, _ in to_claude])
            groups.append([p for p, _ in to_claude])
        except Exception as exc:
            failure = exc
    return texts, to_claude, local, missing, claude, failure, groups


def choose(files):
    """Classify inbox files and validate decisions, logging any unanswered files."""
    tree = folders()
    texts, to_claude, local, missing, claude, failure, _ = ask([(p, extract(p)) for p in files], filing(tree), SHADOW)
    if SHADOW:
        if not failure:
            compare(texts, claude, local, missing, tree)
        decided, unanswered = {p: (d, CLOUD_AI) for p, d in claude.items()}, {}
    else:
        decided = {**{p: (d, LOCAL_AI) for p, d in local.items()}, **{p: (d, CLOUD_AI) for p, d in claude.items()}}
        unanswered = {p: (LOCAL_AI, why) for p, why in missing.items()}
    if not failure:
        unanswered.update({p: (CLOUD_AI, None) for p, _ in to_claude if p not in claude})
    for path, (ai, why) in unanswered.items():
        log(
            f"SANS RÉPONSE {os.path.basename(path)}  ({f'local model: {why}; ' if why else ''}retried on the next pass)"
        )
        activity.log(
            "sans réponse",
            os.path.basename(path),
            src=where(os.path.dirname(path)),
            **ai,
            detail=(f"pas de réponse du modèle local ({why})" if why else "le modèle n'a rien répondu pour ce fichier")
            + " : nouvel essai au prochain passage",
        )
    choices = [outcome(p, decided[p][0], tree) + (decided[p][1],) for p in files if p in decided]
    return choices, failure, sum(ai is LOCAL_AI for ai, _ in unanswered.values())
