#!/usr/bin/env python3
"""Checks of file-sorter, offline and without touching anything real: python3 -I tests.py
The server's pass (sort.py --decide) runs in a sandbox: a copy of the code with the example settings, fake iCloud and Drive folders, a fake
claude that answers from a table, a fake local model (the home server's OpenAI API, its /tokenize included) that answers from another,
a fake rclone that moves inside the fake Drive. Checked: the activity journal and the step report the Dashboard reads (activity.py), pass
after pass: decisions, moves in the Drive, the Mac's results, a failing model, a failing Drive; the shadow mode of the server's unit
(FILE_SORTER_SHADOW=1: Claude decides, the local model is compared), then the local model deciding for the files with text, its batches
measured against its 16k-token slot, and the local model down; last, the review of the theme folders (keep, archive, trash): its trial,
its guards (thresholds, copies, what it never reads), the trial lifted, the Mac carrying out archiver and supprimer (sort.py --apply,
a fake trash), the models or a mount out of reach, and its rules of 05/11/2026 evening (from 0.7 out of the folder, the trash from 0.9
and the archive below, three hours of rest, a folder read again whole under new rules); and what the iPhone saves into the Drive without a folder (Chrome's
« Enregistré depuis Chrome », the Drive's root), sorted like an inbox.
The French file names and texts below are test data, as the owner's would be."""
import contextlib, http.server, io, json, os, re, shutil, subprocess, sys, tempfile, threading, time, unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import activity  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("ok    " if cond else "FAIL  ") + name)
    failures += 0 if cond else 1


# ---- activity.py alone ----
with tempfile.TemporaryDirectory() as t:
    activity.PATH = os.path.join(t, "activity.jsonl")
    activity.log("rangé", "a.pdf", src="iCloud/00 À trier", dst="iCloud/30 Santé/a.pdf", detail="x" * 2000, engine="claude", model="haiku",
                 confidence=1.7, empty="")
    ev = json.loads(Path(activity.PATH).read_text())
    check("journal: one event per line, from and to, empty fields left out, detail cut at 1,000, confidence within 0 to 1",
          ev["from"] == "iCloud/00 À trier" and ev["to"].endswith("a.pdf") and "empty" not in ev and len(ev["detail"]) == 1000
          and ev["confidence"] == 1.0 and ev["run"] == activity.RUN and ev["time"][:4].isdigit())
    activity.log("gardé", "b c.pdf")
    lines = Path(activity.PATH).read_text().splitlines()
    check("journal: a line separator inside a name does not split the event", len(lines) == 2 and json.loads(lines[1])["object"] == "b c.pdf")
    check("journal: private file (600)", os.stat(activity.PATH).st_mode & 0o777 == 0o600)
    old = json.dumps({"time": "2020-01-01T00:00:00+01:00", "action": "rangé", "object": "vieux"})
    with open(activity.PATH, "a") as f:
        f.write(old + "\nnot json\n")
    saved = activity.MAX_BYTES
    activity.MAX_BYTES, activity.rotated = 10, False
    activity.log("doublon", "d.pdf")
    activity.MAX_BYTES = saved
    kept = [json.loads(x)["object"] for x in Path(activity.PATH).read_text().splitlines()]
    check("journal: past its size, rewritten with the last 90 days only, a bad line dropped", kept == ["a.pdf", "b c.pdf", "d.pdf"])
    activity.PATH = t   # a folder: the write fails
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        activity.log("rangé", "e.pdf")
        activity.log("rangé", "f.pdf")
    check("journal: a write that fails never raises, and is reported once", err.getvalue().count("activity journal not written") == 1)
    activity.PATH = None
    r = activity.Report(os.path.join(t, "status.json"), ("a", "b", "c"), time="2026-11-03T15:20:00+01:00")
    r.set("a", "ok")
    r.running("b")
    check("report: written at each change, the step running is known",
          json.loads(Path(t, "status.json").read_text()) == {"time": "2026-11-03T15:20:00+01:00", "steps": {"a": "ok", "b": "running"}} and r.current() == "b")
    r.fail("Claude a échoué\n(code 1)")
    r.fail("encore")
    check("report: a failure goes to the step running, else to the first one without a state; one line, French",
          r.steps == {"a": "ok", "b": "failed: Claude a échoué (code 1)", "c": "failed: encore"})
    r = activity.Report(os.path.join(t, "status.json"), ("a", "b"))
    r.running("a")
    r.finish()
    check("report: a run that went through leaves no step running", r.steps == {"a": "ok"} and not list(Path(t).glob(".*.tmp")))

class Local(http.server.BaseHTTPRequestHandler):
    """The local model as sort.py reaches it. /tokenize: a token per two characters. A question: for each file of the prompt, the answer
    the table answers holds for it (none for a file it does not hold). down: 503; refuse_over: a question about more files is refused as
    longer than the slot (400). Each question answered is kept in asked: the files, the prompt's tokens and the answer's reserve."""
    down, refuse_over, answers, asked = False, None, {}, []

    def reply(self, data, code=200):
        out = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            return self.reply({"tokens": list(range((len(body["content"]) + 1) // 2))})
        if Local.down:
            return self.reply({"error": {"code": 503, "message": "Loading model"}}, 503)
        prompt = body["messages"][0]["content"]
        names = re.findall(r"^### (.+)$", prompt, re.M)
        if Local.refuse_over and len(names) > Local.refuse_over:
            return self.reply({"error": {"code": 400, "type": "exceed_context_size_error"}}, 400)
        Local.asked.append({"names": names, "tokens": (len(prompt) + 1) // 2, "max_tokens": body["max_tokens"], "body": body})
        found = [{"fichier": n, **Local.answers[n]} for n in names if n in Local.answers]
        self.reply({"choices": [{"message": {"content": json.dumps({"fichiers": found})}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": (len(prompt) + 1) // 2}})

    def log_message(self, *a):
        pass


LOCAL = http.server.HTTPServer(("127.0.0.1", 0), Local)
threading.Thread(target=LOCAL.serve_forever, daemon=True).start()

# ---- the server's pass in a sandbox ----
T = Path(tempfile.mkdtemp(prefix="file-sorter-tests-"))
code, icloud, drive, data, fake, home = (T / x for x in ("code", "icloud", "drive", "data", "bin", "home"))
for d in (code, data, fake, home / "Library/Logs", icloud / "00 À trier", icloud / "30 Santé", icloud / "50 Travail",
          drive / "00 À trier", drive / "00 Factures entrantes", drive / "00 Pièces jointes"):
    d.mkdir(parents=True, exist_ok=True)
for f in ("sort.py", "activity.py"):
    shutil.copy(HERE / f, code / f)
for example, real in (("settings.example.json", "settings.json"), ("convention.example.md", "convention.md"), ("rules.example.md", "rules.md")):
    shutil.copy(HERE / example, code / real)
(fake / "claude").write_text(f"""#!{sys.executable}
import json, os, re, sys
if os.environ.get("FAKE_CLAUDE_FAIL"):   # what the CLI prints when the subscription's usage limit is reached
    sys.exit(print(json.dumps({{"type": "result", "is_error": True, "api_error_status": 429, "api_error": "usage_limit_reached",
                                "result": "You've hit your 5-hour limit · resets 9:30pm (Europe/Paris)", "usage": {{"input_tokens": 0}}}})) or 1)
prompt = sys.argv[sys.argv.index("-p") + 1]
answers = json.load(open(os.environ["FAKE_ANSWERS"], encoding="utf-8"))
names = re.findall(r"^### (.+)$", prompt, re.M)
open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8").write(json.dumps(names) + "\\n")   # the files Claude was asked about
if os.environ.get("FAKE_CLAUDE_PROMPTS"):   # the whole question and the folders Claude may read
    open(os.environ["FAKE_CLAUDE_PROMPTS"], "a", encoding="utf-8").write(json.dumps({{"prompt": prompt, "dirs": [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--add-dir"]}}) + "\\n")
if os.environ.get("FAKE_CLAUDE_FAIL_ON") in names:   # a file the API refuses (an image it cannot process): the whole question fails
    sys.exit(print(json.dumps({{"type": "result", "is_error": True, "api_error": "invalid_request_error", "result": "Could not process image"}})) or 1)
found = [{{"fichier": n, **answers[n]}} for n in names if n in answers]
print(json.dumps({{"structured_output": {{"fichiers": found}}}}))
""")
(fake / "rclone").write_text(f"""#!{sys.executable}
import os, shutil, sys
command, rest = sys.argv[1], sys.argv[2:]
local = lambda remote: os.path.join(os.environ["FAKE_DRIVE"], remote.split(":", 1)[1])
if os.environ.get("FAKE_RCLONE_FAIL") == command:
    sys.exit(print("googleapi: Error 500: backendError", file=sys.stderr) or 1)
if command == "lsf":
    folder = local(rest[-1])
    if not os.path.isdir(folder):
        sys.exit(print("directory not found", file=sys.stderr) or 3)
    print("\\n".join(n + ("/" if os.path.isdir(os.path.join(folder, n)) else "") for n in sorted(os.listdir(folder))))
elif command == "moveto":
    os.makedirs(os.path.dirname(local(rest[1])), exist_ok=True)
    shutil.move(local(rest[0]), local(rest[1]))
elif command == "deletefile":
    os.remove(local(rest[-1]))
""")
(fake / "notifier").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{T}/notified.txt'\n")   # the phone's notifications, kept for the checks
(fake / "osascript").write_text("#!/bin/sh\nexit 0\n")
(fake / "trash").write_text("#!/bin/sh\nmkdir -p \"$HOME/.Trash\" && mv \"$1\" \"$HOME/.Trash/\"\n")   # the Mac's trash command
for f in fake.iterdir():
    f.chmod(0o755)
answers = T / "answers.json"


def write(path, text):
    path.write_text(text * 3, encoding="utf-8")   # more than 40 characters: sort.py sends the text, not the file


claude_log = T / "claude-asked.jsonl"


def answer(table, local=None):
    """What the fake claude answers, and the fake local model (the same unless local says otherwise)."""
    rows = lambda t: {n: {"destination": dest, "nouveau_nom": new, "confiance": c, "raison": why} for n, (dest, new, c, why) in t.items()}
    answers.write_text(json.dumps(rows(table), ensure_ascii=False), encoding="utf-8")
    Local.answers = rows(table if local is None else local)


def claude_asked():
    """The files claude was asked about, question by question, since the last call."""
    asked = [json.loads(x) for x in claude_log.read_text(encoding="utf-8").splitlines()] if claude_log.exists() else []
    claude_log.unlink(missing_ok=True)
    return asked


def run(*args, **env):
    """One pass of sort.py, by default as the server's unit runs it from 04/11/2026: Claude decides, the local model is compared."""
    e = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "FILE_SORTER_ICLOUD": str(icloud), "FILE_SORTER_DRIVE": str(drive),
         "FILE_SORTER_DATA": str(data), "FILE_SORTER_CLAUDE": str(fake / "claude"), "FILE_SORTER_DRIVE_RCLONE": "gsort:", "FILE_SORTER_MIN_AGE": "0",
         "FILE_SORTER_LOCAL_URL": f"http://127.0.0.1:{LOCAL.server_port}", "FILE_SORTER_SHADOW": "1", "FILE_SORTER_REVIEW_PASS": "1",
         "FAKE_DRIVE": str(drive), "FAKE_ANSWERS": str(answers), "FAKE_CLAUDE_LOG": str(claude_log), **env}
    return subprocess.run([sys.executable, "-I", str(code / "sort.py"), *args], env=e, capture_output=True, text=True, timeout=120)


journal = data / "activity.jsonl"
events = lambda: [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []
report = lambda: json.loads((data / "run-status.json").read_text(encoding="utf-8"))
by = lambda evs, action, obj: next((e for e in evs if e["action"] == action and e["object"] == obj), None)

write(icloud / "30 Santé" / "Ordonnance.txt", "Ordonnance du docteur pour le patient. ")
write(icloud / "30 Santé" / "Analyse.txt", "Résultats d'analyse du laboratoire. ")
write(icloud / "00 À trier" / "copie.txt", "Ordonnance du docteur pour le patient. ")   # the same bytes: a duplicate the Mac throws away
write(drive / "00 À trier" / "double.txt", "Résultats d'analyse du laboratoire. ")   # in the Drive: thrown away by the server
write(drive / "00 À trier" / "Contrat client.docx", "Contrat de prestation de services informatiques. ")
write(icloud / "00 À trier" / "Scan facture.txt", "Facture EDF électricité octobre 2026 montant. ")
write(drive / "00 Factures entrantes" / "Facture inconnue.txt", "Facture d'un émetteur difficile à lire, montant. ")
write(drive / "00 Pièces jointes" / "2026-10-01 Objet - photo.txt", "Une photo de vacances envoyée par un ami. ")
write(drive / "00 À trier" / "Mystère.txt", "Un document dont le modèle ne dira rien du tout. ")
haiku = {"Contrat client.docx": ("50 Travail", "2026-10-01 Client - Contrat.docx", 0.9, "contrat de prestation du client."),
         "Scan facture.txt": ("50 Travail", "2026-10-31 EDF - Facture.txt", 0.93, "facture d'électricité, émetteur EDF"),
         "Facture inconnue.txt": ("50 Travail", "Facture.txt", 0.4, "émetteur illisible"),
         "2026-10-01 Objet - photo.txt": ("30 Santé", "2026-10-01 Ami - Photo.txt", 0.5, "photo personnelle")}
answer(haiku, {**haiku, "Scan facture.txt": ("50 Travail", "2026-10-31 EDF - Facture électricité.txt", 0.9, "facture EDF"),   # another name
               "Facture inconnue.txt": ("30 Santé", "Facture.txt", 0.45, "illisible"),   # also left, another folder proposed: the same outcome
               "2026-10-01 Objet - photo.txt": ("30 Santé", "2026-10-01 Ami - Photo.txt", 0.8, "photo")})   # filed, where Haiku leaves it
p = run("--decide")
ev = events()
check("pass 1: the pass goes through", p.returncode == 0 or print(p.stderr[-2000:]))
e = by(ev, "rangé", "Contrat client.docx")
check("pass 1: an editable document filed by the server in the Drive: « rangé » with where from, where to, the model and its confidence",
      e and e["from"] == "Google Drive/00 À trier" and e["to"] == "Google Drive/50 Travail/2026-10-01 Client - Contrat.docx"
      and e["engine"] == "claude" and e["model"] == "haiku" and e["confidence"] == 0.9
      and e["detail"] == "contrat de prestation du client ; rangé par le serveur dans le Drive"
      and (drive / "50 Travail" / "2026-10-01 Client - Contrat.docx").exists())
e = by(ev, "renommé", "Contrat client.docx")
check("pass 1: its new name, « renommé » from the old name to the new one", e and e["from"] == "Contrat client.docx" and e["to"] == "2026-10-01 Client - Contrat.docx")
e = by(ev, "rangé", "Scan facture.txt")
check("pass 1: a document for iCloud is « rangé » once the plan is written, the Mac moving it next",
      e and e["to"] == "iCloud/50 Travail/2026-10-31 EDF - Facture.txt" and "le Mac le range" in e["detail"] and by(ev, "renommé", "Scan facture.txt"))
e = by(ev, "à trier", "Facture inconnue.txt")
check("pass 1: a doubtful invoice goes to 00 À trier, with the reason", e and e["to"] == "Google Drive/00 À trier/Facture inconnue.txt"
      and "pas assez sûr (40 %, il faut 70 %)" in e["detail"] and "émetteur illisible" in e["detail"] and e["confidence"] == 0.4)
e = by(ev, "gardé", "2026-10-01 Objet - photo.txt")
check("pass 1: a doubtful attachment is « gardé » where it is, with the reason", e and e["from"] == "Google Drive/00 Pièces jointes" and "photo personnelle" in e["detail"])
e = by(ev, "doublon", "double.txt")
check("pass 1: a duplicate in the Drive goes to the Drive's trash, its original named",
      e and e["to"] == "corbeille du Drive" and "iCloud/30 Santé/Analyse.txt" in e["detail"] and not (drive / "00 À trier" / "double.txt").exists())
e = by(ev, "doublon", "copie.txt")
check("pass 1: a duplicate in iCloud is left to the Mac", e and e["to"] == "Corbeille du Mac" and "iCloud/30 Santé/Ordonnance.txt" in e["detail"])
check("pass 1: a file the model skipped is « sans réponse »", by(ev, "sans réponse", "Mystère.txt"))
check("pass 1: one run for all the events", len({x["run"] for x in ev}) == 1 and len(ev) == 9)
check("pass 1: the step report", report()["steps"] == {"inboxes": "ok", "decide": "ok", "drive": "ok", "plan": "ok", "icloud": "skipped"}
      and report()["time"] == ev[0]["run"])
shadow = [x.split("  ", 1)[1] for x in p.stdout.splitlines() if "  shadow: " in x]
check("shadow: Claude decides for every file, the journal says so",
      all(x.get("engine") == "claude" for x in ev if "model" in x) and [sorted(x) for x in claude_asked()]
      == [sorted(["Contrat client.docx", "Scan facture.txt", "Facture inconnue.txt", "2026-10-01 Objet - photo.txt", "Mystère.txt"])])
check("shadow: the local model gets the files with text only, in one question", [sorted(x["names"]) for x in Local.asked]
      == [sorted(["Scan facture.txt", "Facture inconnue.txt", "2026-10-01 Objet - photo.txt", "Mystère.txt"])])
check("shadow: one line per file the two would not file the same way, folder or name",
      "shadow: Scan facture.txt  haiku=50 Travail/2026-10-31 EDF - Facture.txt (0.93)  local=50 Travail/2026-10-31 EDF - Facture électricité.txt (0.90)" in shadow
      and any(x.startswith("shadow: 2026-10-01 Objet - photo.txt  haiku=stays (0.50") and x.endswith("local=30 Santé/2026-10-01 Ami - Photo.txt (0.80)") for x in shadow)
      and not any("Facture inconnue.txt" in x for x in shadow))
check("shadow: a summary of the comparison, and the prompt's size measured", "shadow: 3 file(s) with text compared: 1 the same, 2 different, 0 without a local answer" in shadow
      and any(x.startswith("shadow: local model qwen3.5-9b: 3 of 4 file(s) answered, ") and x.endswith(" prompt tokens") for x in shadow))
Local.asked.clear()

plan = json.loads((data / "plan.json").read_text(encoding="utf-8"))
ids = {a["type"]: a["id"] for a in plan["actions"]}
(icloud / "00 À trier" / "Scan facture.txt").rename(icloud / "50 Travail" / "2026-10-31 EDF - Facture.txt")   # what the Mac did
(data / "results.json").write_text(json.dumps({"appliques": {ids["ranger"]: "ok", ids["doublon"]: "erreur : PermissionError Operation not permitted"}}))
p = run("--decide")
new = events()[len(ev):]
e = by(new, "rangé par le Mac", "Scan facture.txt")
check("pass 2: what the Mac did comes back once: « rangé par le Mac »", e and e["from"] == "iCloud/00 À trier" and e["to"] == "iCloud/50 Travail/2026-10-31 EDF - Facture.txt")
e = by(new, "échec", "copie.txt")
check("pass 2: and what it could not do: « échec », with its error", e and e["detail"] == "le Mac n'a pas pu le mettre à la Corbeille : PermissionError Operation not permitted")
check("pass 2: the Mac's step failed, without a file name", report()["steps"]["icloud"] == "failed: le Mac n'a pas pu appliquer 1 action(s) du plan")
p = run("--decide")
check("pass 3: a result already logged is not logged again", not [x for x in events() if x["action"] in ("rangé par le Mac", "échec")][2:])

n = len(events())
write(icloud / "00 À trier" / "Nouveau.txt", "Un nouveau document arrivé dans iCloud. ")
claude_asked()
p = run("--decide", FAKE_CLAUDE_FAIL="1")
new = events()[n:]
check("model down: the pass fails at the step « decide », French message without a file name, the cause read from Claude's answer",
      p.returncode != 0 and report()["steps"]["decide"] == "failed: limite d'usage de Claude atteinte, reprise vers 21:30" and report()["steps"]["inboxes"] == "ok")
check("model down: one « échec » with the error in the journal, on one line", [x["action"] for x in new if x["object"] == "passage du tri"] == ["échec"]
      and next(x for x in new if x["action"] == "échec")["detail"] == "limite d'usage de Claude atteinte, reprise vers 21:30 ; RuntimeError : "
      "claude failed (1): usage_limit_reached : You've hit your 5-hour limit · resets 9:30pm (Europe/Paris)")
p = run("--decide", FAKE_CLAUDE_FAIL="1")
check("model still down: no second « échec » for the same series", [x["action"] for x in events()[n:] if x["object"] == "passage du tri"] == ["échec"])
answer({"Nouveau.txt": ("30 Santé", "2026-11-03 Médecin - Courrier.txt", 0.8, "courrier médical")})
p = run("--decide")
new = events()[n + 1:]
check("model back: « rétabli », then the decisions again", p.returncode == 0 and "rétabli" in [x["action"] for x in new] and by(new, "rangé", "Nouveau.txt"))

n = len(events())
write(drive / "00 À trier" / "Devis.docx", "Devis pour des travaux de peinture. ")
answer({"Devis.docx": ("50 Travail", "2026-11-03 Artisan - Devis.docx", 0.85, "devis de travaux")})
p = run("--decide", FAKE_RCLONE_FAIL="moveto")
new = events()[n:]
check("Drive refusing a move: the pass fails at the step « drive », the plan is still written",
      p.returncode == 1 and report()["steps"]["drive"].startswith("failed: 1 déplacement(s) dans le Drive en échec") and report()["steps"]["plan"] in ("ok", "skipped"))
devis = [x for x in new if x["object"] == "Devis.docx"]   # Mystère.txt, never answered, is « sans réponse » at each pass
check("Drive refusing a move: « échec » for that file, with Google's error, and nothing else about it",
      [x["action"] for x in devis] == ["échec"] and "backendError" in devis[0]["detail"] and (drive / "00 À trier" / "Devis.docx").exists())

n = len(events())
(T / "report.md").write_text("# Rapport\n")
p = run("--deposit", "icloud", "50 Travail/2026-11-03 Rapport.md", str(T / "report.md"))
check("--deposit (the VAT check's report for the Mac) writes no event", p.returncode == 0 and len(events()) == n)
check("no file left behind by the atomic writes", not list(data.glob("*.tmp")) and not list(data.glob(".*.tmp")))

# ---- without the shadow: the local model decides for the files with text, Claude only reads what has none ----
for f in list((icloud / "00 À trier").iterdir()) + list((drive / "00 À trier").iterdir()) + list((drive / "00 Pièces jointes").iterdir()):
    f.unlink()
(data / "kept.json").write_text("[]")
write(icloud / "00 À trier" / "Ordonnance.txt", "Ordonnance du docteur Martin, Rennes, 02/11/2026. ")
(icloud / "00 À trier" / "Photo carte.jpg").write_bytes(b"\xff\xd8\xff\xe0 not a text")   # no text: Claude reads it
answer({"Photo carte.jpg": ("30 Santé", "2026-11-02 CPAM - Carte Vitale.jpg", 0.9, "carte Vitale"),
        "Ordonnance.txt": ("30 Santé", "2026-11-02 Dr Martin - Ordonnance.txt", 0.95, "ordonnance")})
n, _ = len(events()), claude_asked()
Local.asked.clear()
p = run("--decide", FILE_SORTER_SHADOW="")
new = events()[n:]
e = by(new, "rangé", "Ordonnance.txt")
check("local model: it decides for a file with text, and the journal names it", p.returncode == 0 and e and e["engine"] == "local"
      and e["model"] == "qwen3.5-9b" and e["confidence"] == 0.95 and e["to"] == "iCloud/30 Santé/2026-11-02 Dr Martin - Ordonnance.txt")
e = by(new, "rangé", "Photo carte.jpg")
check("local model: a file without text still goes to Claude, and only it", e and e["engine"] == "claude" and e["model"] == "haiku"
      and claude_asked() == [["Photo carte.jpg"]] and [x["names"] for x in Local.asked] == [["Ordonnance.txt"]])
sent = Local.asked[0]["body"]
fichiers = sent["response_format"]["json_schema"]["schema"]["properties"]["fichiers"]
check("local model: strict schema, one answer per file, the names asked only, temperature 0, no thinking, no tool, the line on « confiance »",
      sent["response_format"]["json_schema"]["strict"] is True and fichiers["minItems"] == fichiers["maxItems"] == 1
      and fichiers["items"]["properties"]["fichier"]["enum"] == ["Ordonnance.txt"] and sent["temperature"] == 0
      and sent["chat_template_kwargs"] == {"enable_thinking": False} and "tools" not in sent
      and "ta certitude sur la destination et le nom que tu proposes" in sent["messages"][0]["content"])
check("local model: no « shadow: » line without the shadow", "shadow:" not in p.stdout)

n = len(events())
write(icloud / "00 À trier" / "Résultats.txt", "Résultats d'analyse sanguine du laboratoire, 03/11/2026. ")
answer({"Résultats.txt": ("30 Santé", "2026-11-03 Laboratoire - Résultats.txt", 0.9, "analyse")})
Local.down = True
p = run("--decide", FILE_SORTER_SHADOW="")
Local.down = False
new = events()[n:]
e = by(new, "sans réponse", "Résultats.txt")
check("local model down: the file stays, « sans réponse », and no other model is asked", p.returncode == 0 and e and e["engine"] == "local"
      and "pas de réponse du modèle local (injoignable, HTTP 503) : nouvel essai au prochain passage" == e["detail"]
      and (icloud / "00 À trier" / "Résultats.txt").exists() and not claude_asked() and not by(new, "rangé", "Résultats.txt"))
check("local model down: the step « decide » says so, without a file name, and the pass goes on",
      report()["steps"]["decide"] == "failed: le modèle local n'a pas répondu pour 1 fichier(s) : nouvel essai au prochain passage"
      and report()["steps"]["plan"] in ("ok", "skipped") and "SANS RÉPONSE Résultats.txt" in p.stdout)
p = run("--decide", FILE_SORTER_SHADOW="")
check("local model back: the file is decided at the next pass", by(events()[n:], "rangé", "Résultats.txt") and report()["steps"]["decide"] == "ok")
p = run("--decide", FILE_SORTER_SHADOW="", FILE_SORTER_LOCAL_URL="off")
check("local model off: no question at all", p.returncode == 0)

# ---- the local model's batches: measured with its tokenizer against a 16k-token slot, 5 files at most ----
for f in (icloud / "00 À trier").iterdir():
    f.unlink()
(data / "plan.json").unlink(missing_ok=True)
(data / "kept.json").write_text("[]")
figures = "Facture n° FA-2026-10-4471 du 31/10/2026, client 6004 512 873, 412 kWh, HT 68,42 €, TVA 13,68 €, TTC 82,10 €. "
names = [f"Facture {i:02d}.txt" for i in range(12)]
for i, name in enumerate(names):   # each its own figures (identical files would be duplicates), more than 2,500 characters: cut to 2,500
    (icloud / "00 À trier" / name).write_text((figures.replace("4471", f"44{i:02d}") * 30)[:2600], encoding="utf-8")
answer({name: ("50 Travail", f"2026-10-31 EDF - Facture {name[8:10]}.txt", 0.9, "facture") for name in names})


def batches(folders):
    """One pass on the twelve invoices with a tree of that many folders: [(files, prompt tokens, answer's reserve)] of each question."""
    shutil.rmtree(icloud / "40 Logement", ignore_errors=True)
    for i in range(folders):
        (icloud / "40 Logement" / f"Dossier {i:04d}").mkdir(parents=True, exist_ok=True)
    (data / "plan.json").unlink(missing_ok=True)
    Local.asked.clear()
    p = run("--decide", FILE_SORTER_SHADOW="")
    return p, [(len(x["names"]), x["tokens"], x["max_tokens"]) for x in Local.asked]


p, seen = batches(150)
print(f"measure: 150 folders, 2,500 characters a file: {[x[0] for x in seen]} files a question, prompts of {[x[1] for x in seen]} tokens")
check("batches: a usual tree, 5 files a question, each within the slot", p.returncode == 0 and [x[0] for x in seen] == [5, 5, 2]
      and all(tokens + reserve + 256 <= 16384 for _, tokens, reserve in seen))
p, seen = batches(700)
print(f"measure: 700 folders, 2,500 characters a file: {[x[0] for x in seen]} files a question, prompts of {[x[1] for x in seen]} tokens")
check("batches: a big tree, fewer files a question (3 here), each within the slot", p.returncode == 0 and max(x[0] for x in seen) == 3
      and sum(x[0] for x in seen) == 12 and all(tokens + reserve + 256 <= 16384 for _, tokens, reserve in seen))
p, seen = batches(2000)
check("batches: a tree too big for even one file, each file waits « sans réponse », nothing asked", p.returncode == 0 and not seen
      and "trop long pour un créneau" in by(events(), "sans réponse", "Facture 00.txt")["detail"])
Local.refuse_over = 2
p, seen = batches(150)
Local.refuse_over = None
check("batches: a question refused as too long is asked again in halves", p.returncode == 0 and max(x[0] for x in seen) <= 2 and sum(x[0] for x in seen) == 12)

# ---- the review of the theme folders: keep, archive or trash what is already filed (FILE_SORTER_CLEANUP: trial by default, then on) ----
R = T / "review"
icloud2, drive2, data2 = R / "icloud", R / "drive", R / "data"
for d in (data2, icloud2 / "00 À trier", drive2 / "00 À trier", drive2 / "00 Factures entrantes", drive2 / "00 Pièces jointes"):
    d.mkdir(parents=True, exist_ok=True)
ident, achats = icloud2 / "10 Identité & Banque" / "Identité", icloud2 / "60 Achats" / "Commandes"
bail, factures, contrats = icloud2 / "20 Administratif" / "Logement" / "Ancien bail", icloud2 / "50 Travail" / "Factures 2019", drive2 / "50 Travail" / "Contrats"
ident.mkdir(parents=True)
write(ident / "2015-06-01 Préfecture - Carte d'identité.txt", "Carte nationale d'identité délivrée le 01/06/2015, expire le 01/06/2025. ")
write(ident / "2024-05-02 Préfecture - Carte d'identité.txt", "Carte nationale d'identité délivrée le 02/05/2024, valable jusqu'en 2034. ")
shutil.copy(ident / "2024-05-02 Préfecture - Carte d'identité.txt", ident / "2024-05-02 Préfecture - Carte d'identité (2).txt")   # the same bytes
write(ident / "Pré-demande ANTS.txt", "Pré-demande de carte d'identité, numéro à présenter au rendez-vous du 12/04/2024. ")
write(ident / "Récépissé.txt", "Récépissé de dépôt d'une demande de titre, 12/04/2024. ")
for i, name in enumerate(("CNI recto.jpg", "CNI recto flou.jpg", "Carte identité photo.jpg", "Permis A.jpg", "Permis B.jpg")):
    (ident / name).write_bytes(b"\xff\xd8\xff\xe0 image " + bytes([i]) * 50)   # no text: Claude reads them
bail.mkdir(parents=True)
write(bail / "2018-07-01 Agence - Bail.txt", "Bail d'habitation du 01/07/2018, résilié le 30/06/2021. ")
factures.mkdir(parents=True)
invoices = [f"2019-{m:02d}-28 EDF - Facture.txt" for m in range(1, 13)]
for i, name in enumerate(invoices):
    write(factures / name, f"Facture EDF n° {i} du 28/{i + 1:02d}/2019, électricité, montant. ")
achats.mkdir(parents=True)
write(achats / "CGV Boutique.txt", "Conditions générales de vente de la boutique, article 1, article 2. ")
write(achats / "2026-10-02 Boutique - Facture.txt", "Facture de la boutique du 02/10/2026, montant TTC. ")
contrats.mkdir(parents=True)
write(contrats / "2019-01-01 Ancien employeur - Contrat.txt", "Contrat de travail du 01/01/2019, terminé le 31/12/2022. ")
write(contrats / "Conditions générales.txt", "Conditions générales d'utilisation du service en ligne. ")
write(contrats / "2026-01-01 Client - Contrat.txt", "Contrat de prestation en cours avec le client, 2026. ")
never = {"IMG_0001.jpg": icloud2 / "80 Images" / "Photos", "Ancienne.txt": icloud2 / "90 Archives" / "10 Identité & Banque", "Raccourci.txt": icloud2 / "Shortcuts",
         "Sauvegarde.txt": drive2 / "90 Archives" / "Sauvegardes", "Vrac.txt": icloud2, "Module.txt": icloud2 / "50 Travail" / "Code" / "src"}
for name, folder in never.items():   # never read by the review: photos, the archive, a folder not numbered, the root, a code project
    folder.mkdir(parents=True, exist_ok=True)
    write(folder / name, "Un fichier que la relecture ne doit jamais lire. ")
old = time.time() - 10 * 86400
for f in R.rglob("*"):
    if f.is_file():
        os.utime(f, (old, old))
write(achats / "Nouveau.txt", "Un document modifié aujourd'hui, laissé pour plus tard. ")   # too recent
renv = {"FILE_SORTER_ICLOUD": str(icloud2), "FILE_SORTER_DRIVE": str(drive2), "FILE_SORTER_DATA": str(data2), "FAKE_DRIVE": str(drive2)}
rjournal = data2 / "activity.jsonl"
revents = lambda: [json.loads(x) for x in rjournal.read_text(encoding="utf-8").splitlines()] if rjournal.exists() else []
rreport = lambda: json.loads((data2 / "run-status.json").read_text(encoding="utf-8"))
rstate = lambda: json.loads((data2 / "review.json").read_text(encoding="utf-8"))
rplan = lambda: json.loads((data2 / "plan.json").read_text(encoding="utf-8"))


def rrun(*args, **env):
    return run(*args, **{**renv, **env})


def review_answers(table):
    """What the two fake models answer to the review's question: {name: (decision, doublon_de, confiance, raison)}."""
    rows = {n: {"raison": why, "decision": dec, "doublon_de": twin, "confiance": c} for n, (dec, twin, c, why) in table.items()}
    answers.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    Local.answers = rows


review_answers({"2015-06-01 Préfecture - Carte d'identité.txt": ("archiver", None, 0.92, "carte expirée en 2025, remplacée par celle de 2024."),
                "2024-05-02 Préfecture - Carte d'identité.txt": ("garder", None, 0.95, "carte valable"),
                "Pré-demande ANTS.txt": ("supprimer", None, 0.95, "pré-demande dont le rendez-vous est passé"),
                "Récépissé.txt": ("archiver", None, 0.6, "récépissé ancien"),   # not sure enough: it stays
                "CNI recto.jpg": ("garder", None, 0.9, "scan net de la carte"),
                "CNI recto flou.jpg": ("supprimer", "CNI recto.jpg", 0.95, "même carte, plus floue"),
                "Carte identité photo.jpg": ("supprimer", "2024-05-02 Préfecture - Carte d'identité.txt", 0.95, "photo de la carte"),   # not in its question
                "Permis A.jpg": ("supprimer", "Permis B.jpg", 0.95, "même permis"), "Permis B.jpg": ("supprimer", "Permis A.jpg", 0.95, "même permis"),
                "2018-07-01 Agence - Bail.txt": ("archiver", None, 0.9, "bail résilié en 2021"),
                **{name: ("archiver", None, 0.9, "facture de 2019") for name in invoices},
                "CGV Boutique.txt": ("supprimer", None, 0.97, "conditions générales de vente sans intérêt"),
                "2026-10-02 Boutique - Facture.txt": ("garder", None, 0.9, "facture récente"),
                "2019-01-01 Ancien employeur - Contrat.txt": ("archiver", None, 0.9, "contrat terminé en 2022"),
                "Conditions générales.txt": ("supprimer", None, 0.95, "conditions d'utilisation génériques"),
                "2026-01-01 Client - Contrat.txt": ("garder", None, 0.95, "contrat en cours")})
claude_asked()
Local.asked.clear()
(T / "notified.txt").unlink(missing_ok=True)
p = rrun("--decide")
ev = revents()
check("review, trial: the pass goes through, the first theme folder read whole (10 documents), in one « revu »", p.returncode == 0 and
      [(e["object"], e["detail"]) for e in ev if e["action"] == "revu"] == [("iCloud/10 Identité & Banque/Identité",
                                                                             "10 document(s) relu(s) : 5 gardé(s), 1 à archiver, 4 à supprimer ; essai : rien n'a bougé")]
      or print(p.stdout[-3000:], p.stderr[-3000:]))
e = by(ev, "à archiver", "2015-06-01 Préfecture - Carte d'identité.txt")
check("review, trial: an expired card « à archiver », to the archive at the same path, by the local model, nothing moved",
      e and e["from"] == "iCloud/10 Identité & Banque/Identité" and e["to"] == "iCloud/90 Archives/10 Identité & Banque/Identité"
      and e["detail"] == "essai, rien n'a bougé : carte expirée en 2025, remplacée par celle de 2024" and e["engine"] == "local" and e["confidence"] == 0.92
      and (ident / "2015-06-01 Préfecture - Carte d'identité.txt").exists())
e = by(ev, "à supprimer", "2024-05-02 Préfecture - Carte d'identité (2).txt")
check("review, trial: an exact copy « à supprimer », its original named, without asking a model", e and e["to"] == "Corbeille du Mac"
      and e["detail"] == "essai, rien n'a bougé : copie identique de « 2024-05-02 Préfecture - Carte d'identité.txt »" and "engine" not in e)
e = by(ev, "à supprimer", "CNI recto flou.jpg")
check("review, trial: a lesser copy seen in the same question « à supprimer », by Claude, the one kept named",
      e and e["engine"] == "claude" and e["detail"] == "essai, rien n'a bougé : copie moins bonne de « CNI recto.jpg » : même carte, plus floue")
check("review, trial: a file of no more use « à supprimer »", by(ev, "à supprimer", "Pré-demande ANTS.txt"))
check("review, trial: kept, a copy of a file its model did not see, a decision not sure enough, and one of two copies naming each other",
      not any(by(ev, a, n) for a in ("à archiver", "à supprimer") for n in ("Carte identité photo.jpg", "Récépissé.txt", "Permis A.jpg", "CNI recto.jpg"))
      and by(ev, "à supprimer", "Permis B.jpg")["detail"].startswith("essai, rien n'a bougé : copie moins bonne de « Permis A.jpg »"))
check("review, trial: the files with text to the local model even under the shadow, the others to Claude, the copy to none",
      [sorted(x["names"]) for x in Local.asked] == [sorted(["2015-06-01 Préfecture - Carte d'identité.txt", "2024-05-02 Préfecture - Carte d'identité.txt",
                                                            "Pré-demande ANTS.txt", "Récépissé.txt"])]
      and [sorted(x) for x in claude_asked()] == [sorted(["CNI recto.jpg", "CNI recto flou.jpg", "Carte identité photo.jpg", "Permis A.jpg", "Permis B.jpg"])])
sent = Local.asked[0]["body"]
item = sent["response_format"]["json_schema"]["schema"]["properties"]["fichiers"]["items"]["properties"]
check("review, trial: the question names the folder and lists all its files; strict schema, the reason before the decision, a closed list",
      "Dossier relu : « 10 Identité & Banque/Identité »" in sent["messages"][0]["content"] and "- Permis B.jpg (1 Ko)" in sent["messages"][0]["content"]
      and list(item) == ["fichier", "raison", "decision", "doublon_de", "confiance"] and item["decision"]["enum"] == ["garder", "archiver", "supprimer"]
      and "### Récépissé.txt" in sent["messages"][0]["content"] and "### CNI recto.jpg" not in sent["messages"][0]["content"])
check("review, trial: nothing in the plan, every file still there, the step « decide » ok",
      not rplan()["actions"] and all((ident / n).exists() for n in ("Pré-demande ANTS.txt", "CNI recto flou.jpg", "Permis B.jpg"))
      and rreport()["steps"]["decide"] == "ok")
st = rstate()
check("review, trial: each decision kept in review.json, the proposals waiting", len(st["files"]) == 10
      and sum(e["state"] == "pending" for e in st["files"].values()) == 5 and st["cursor"] == [0, "10 Identité & Banque", "Identité"])

Local.asked.clear()
for _ in range(6):   # five folders more, then the end of the walk
    p = rrun("--decide")
ev = revents()
check("review, trial: one folder a pass, a big one in two (10, then 2), each in its « revu »",
      [e["object"] for e in ev if e["action"] == "revu"] == ["iCloud/10 Identité & Banque/Identité", "iCloud/20 Administratif/Logement/Ancien bail",
                                                             "iCloud/50 Travail/Factures 2019", "iCloud/50 Travail/Factures 2019", "iCloud/60 Achats/Commandes",
                                                             "Google Drive/50 Travail/Contrats"]
      and [x["detail"].split(" :")[0] for x in ev if x["object"] == "iCloud/50 Travail/Factures 2019"] == ["10 document(s) relu(s)", "2 document(s) relu(s)"])
check("review, trial: a big folder's question lists its files around those asked", all(tokens + reserve + 256 <= 16384 for tokens, reserve in
      [(x["tokens"], x["max_tokens"]) for x in Local.asked]) and any("2019-12-28 EDF - Facture.txt (1 Ko)" in x["body"]["messages"][0]["content"] for x in Local.asked))
e = by(ev, "à archiver", "Conditions générales.txt")
check("review, trial: nothing of the Drive to the trash: its terms of use « à archiver », saying why, like the old contract",
      e and e["from"] == "Google Drive/50 Travail/Contrats" and e["to"] == "Google Drive/90 Archives/50 Travail/Contrats"
      and e["detail"] == "essai, rien n'a bougé : conditions d'utilisation génériques ; à l'archive plutôt qu'à la corbeille : rien n'est supprimé dans le Drive"
      and not by(ev, "à supprimer", "Conditions générales.txt") and not [x for x in ev if x["action"] == "à supprimer" and x["from"].startswith("Google Drive")]
      and by(ev, "à archiver", "2019-01-01 Ancien employeur - Contrat.txt")["to"] == "Google Drive/90 Archives/50 Travail/Contrats")
asked_names = {n for x in Local.asked for n in x["names"]} | {n for x in claude_asked() for n in x}
check("review: never the photos, the archive, a folder not numbered, the root, a code project, the inboxes, nor a file of the last two days",
      not asked_names & (set(never) | {"Nouveau.txt"}) and not [x for x in ev if x["object"] in never or x["object"] == "Nouveau.txt"])
check("review: a document of the last two days is listed to the model with the others, not asked about",
      any("- Nouveau.txt (1 Ko)" in x["body"]["messages"][0]["content"] for x in Local.asked))
notified = (T / "notified.txt").read_text(encoding="utf-8").splitlines() if (T / "notified.txt").exists() else []
check("review, trial: the end of the first walk notified once, counts only, no file name",
      notified == ["Tri des fichiers Relecture d'essai des dossiers terminée : 28 documents relus, 16 à archiver et 5 à supprimer proposés, rien n'a bougé. "
                   "Le détail est dans le journal d'activité"])
n = len(revents())
Local.asked.clear()
p = rrun("--decide")
check("review: once every folder is read, the next walk waits an hour: no question, no event", p.returncode == 0 and not Local.asked
      and not claude_asked() and len(revents()) == n)


# the bilan: what review.json holds, folder by folder, in the unit's journal, once per REVIEW_BILAN (the coding agents cannot open it)
def bilan(out):
    """The BILAN lines of a pass: {(root, folder): {name: [code, …]}}, the parts (i, n, characters of JSON) and the totals."""
    found, parts = {}, []
    for i, total, js in re.findall(r"BILAN   (\d+)/(\d+) (.+)$", out, re.M):
        parts.append((int(i), int(total), len(js)))
        for piece in json.loads(js):
            found.setdefault((piece["r"], piece["f"]), {}).update({d[0]: d[1:] for d in piece["d"]})
    end = re.search(r"BILAN   end: (.+)$", out, re.M)
    return found, parts, end and end.group(1)


def bilan_again(**env):
    st = rstate()
    st.pop("bilan", None)
    (data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return rrun("--decide", **env)


p = rrun("--decide")
check("bilan: logged at the review's first pass, not again at the next ones", p.returncode == 0 and "BILAN" not in p.stdout
      and rstate().get("bilan") == "2026-11-06")
n = len(revents())
p = bilan_again()
found, parts, end = bilan(p.stdout)
idf, ctr = found.get(("icloud", "10 Identité & Banque/Identité"), {}), found.get(("drive", "50 Travail/Contrats"), {})
check("bilan: each document of the theme folders with what becomes of it, from review.json, no model asked, nothing in the journal",
      p.returncode == 0 and not Local.asked and not claude_asked() and len(revents()) == n and [x[:2] for x in parts] == [(1, 1)]
      and idf.get("2015-06-01 Préfecture - Carte d'identité.txt") == ["a", 0.92, "carte expirée en 2025, remplacée par celle de 2024"]
      and idf.get("Pré-demande ANTS.txt") == ["s", 0.95, "pré-demande dont le rendez-vous est passé"]
      and idf.get("2024-05-02 Préfecture - Carte d'identité (2).txt") == ["s", 1.0, "copie identique de « 2024-05-02 Préfecture - Carte d'identité.txt »"]
      and idf.get("CNI recto flou.jpg") == ["s", 0.95, "copie moins bonne de « CNI recto.jpg » : même carte, plus floue"]
      and idf.get("CNI recto.jpg") == ["g", 0.9] and idf.get("Récépissé.txt") == ["g", 0.6, "archiver", "récépissé ancien"]
      and idf.get("Carte identité photo.jpg") == ["g", 0.95, "supprimer", "photo de la carte"]
      and ctr.get("Conditions générales.txt") == ["a", 0.95, "conditions d'utilisation génériques ; à l'archive plutôt qu'à la corbeille : rien n'est supprimé dans le Drive"]
      and found.get(("icloud", "60 Achats/Commandes"), {}).get("Nouveau.txt") == ["n"]
      or print(p.stdout[-3000:]))
check("bilan: never the photos, the archive, the root, a code project nor the inboxes; the totals in one line",
      not {name for docs in found.values() for name in docs} & set(never)
      and end == "29 document(s), 7 kept, 16 to archive, 5 to trash, 1 not read yet (FILE_SORTER_CLEANUP=trial)")
source = (code / "sort.py").read_text(encoding="utf-8")
try:   # short lines: a folder goes on in the next one
    (code / "sort.py").write_text(source.replace("REVIEW_BILAN_LINE = 16000", "REVIEW_BILAN_LINE = 500"), encoding="utf-8")
    p = bilan_again()
finally:
    (code / "sort.py").write_text(source, encoding="utf-8")
split, parts, end2 = bilan(p.stdout)
check("bilan: past its length a line goes on in the next one, a folder split too, each part numbered; the same bilan put together",
      p.returncode == 0 and split == found and end2 == end and len(parts) > 4 and all(x[2] <= 510 for x in parts)
      and [x[:2] for x in parts] == [(i, len(parts)) for i in range(1, len(parts) + 1)]
      and sum('"f":"50 Travail/Factures 2019"' in line for line in p.stdout.splitlines() if "BILAN" in line) > 1)

# what the trial proposed to trash in the Drive before the owner's rule of 06/11/2026 goes into its archive instead, said once
st = rstate()
key = next(k for k, e in st["files"].items() if e["path"].endswith("Conditions générales.txt"))
st["files"][key].update(decision="supprimer", proposal="supprimer")
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
n = len(revents())
p = rrun("--decide")
e = rstate()["files"][key]
check("Drive: a trial's proposal to trash there becomes one to archive, a « revu » says so, the model's decision kept",
      p.returncode == 0 and (e["decision"], e["proposal"], e["state"]) == ("archiver", "supprimer", "pending")
      and [(x["action"], x["object"], x["detail"]) for x in revents()[n:]]
      == [("revu", "Google Drive", "1 document(s) proposé(s) pour la corbeille partent plutôt aux archives : rien n'est supprimé dans le Drive")])
p = rrun("--decide")
check("Drive: said once", p.returncode == 0 and len(revents()) == n + 1)
n = len(revents())

# the trial lifted: the trial's decisions carried out, the Drive's by the server, iCloud's by the Mac
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
new = revents()[n:]
e = by(new, "archivé", "2019-01-01 Ancien employeur - Contrat.txt")
check("review, on: the server archives in the Drive, at the same path under 90 Archives", e and e["to"] == "Google Drive/90 Archives/50 Travail/Contrats"
      and e["detail"] == "contrat terminé en 2022 ; archivé par le serveur dans le Drive" and (drive2 / "90 Archives/50 Travail/Contrats/2019-01-01 Ancien employeur - Contrat.txt").exists()
      and not (contrats / "2019-01-01 Ancien employeur - Contrat.txt").exists())
e = by(new, "archivé", "Conditions générales.txt")
check("review, on: the server archives the Drive's terms of use, never into its trash", e and e["to"] == "Google Drive/90 Archives/50 Travail/Contrats"
      and e["detail"] == "conditions d'utilisation génériques ; à l'archive plutôt qu'à la corbeille : rien n'est supprimé dans le Drive ; archivé par le serveur dans le Drive"
      and (drive2 / "90 Archives/50 Travail/Contrats/Conditions générales.txt").exists() and not (contrats / "Conditions générales.txt").exists()
      and not by(new, "supprimé", "Conditions générales.txt") and (contrats / "2026-01-01 Client - Contrat.txt").exists())
kinds = sorted((a["type"], a["source"]["chemin"].rsplit("/", 1)[1]) for a in rplan()["actions"])
check("review, on: iCloud's decisions go into the Mac's plan (archiver, supprimer), all applied at once (21 at most 40)", len(kinds) == 19 and
      [k for k in kinds if k[0] == "supprimer"] == [("supprimer", n) for n in sorted(["2024-05-02 Préfecture - Carte d'identité (2).txt", "CGV Boutique.txt",
                                                                                     "CNI recto flou.jpg", "Permis B.jpg", "Pré-demande ANTS.txt"])]
      and by(new, "supprimé", "CGV Boutique.txt")["detail"] == "conditions générales de vente sans intérêt ; le Mac le met à la Corbeille à son prochain passage"
      and by(new, "archivé", "2018-07-01 Agence - Bail.txt")["to"] == "iCloud/90 Archives/20 Administratif/Logement/Ancien bail")

p = rrun("--decide", FILE_SORTER_CLEANUP="off")
check("review off again: the review's actions the Mac has not done leave its plan, their decisions wait in review.json", p.returncode == 0
      and not [a for a in rplan()["actions"] if a["type"] in ("archiver", "supprimer")]
      and sum(e.get("state") == "pending" for e in rstate()["files"].values()) == 19)
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
check("review on again: they come back into the plan, a copy with the one kept in its place",
      len([a for a in rplan()["actions"] if a["type"] in ("archiver", "supprimer")]) == 19
      and next(a for a in rplan()["actions"] if a["source"]["chemin"].endswith("CNI recto flou.jpg"))["garde"]
      == {"racine": "icloud", "chemin": "10 Identité & Banque/Identité/CNI recto.jpg"})

# while the Mac has not applied them, its documents are not read again, nor what the server already moved (the mount a few minutes behind)
write(ident / "Attestation.txt", "Attestation de domicile pour la préfecture, 2024. ")
write(contrats / "Avenant.txt", "Avenant au contrat de prestation du client, 2026. ")
lagging = contrats / "2019-01-01 Ancien employeur - Contrat.txt"
shutil.copy2(drive2 / "90 Archives/50 Travail/Contrats/2019-01-01 Ancien employeur - Contrat.txt", lagging)   # as the mount may still show it
for f in (ident / "Attestation.txt", contrats / "Avenant.txt", lagging):
    os.utime(f, (old, old))
st = rstate()
st["rest_until"] = 0
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
kept_names = ("2024-05-02 Préfecture - Carte d'identité.txt", "CNI recto.jpg", "Permis A.jpg", "Carte identité photo.jpg", "Récépissé.txt", "Attestation.txt",
              "2026-01-01 Client - Contrat.txt", "Avenant.txt")
review_answers({x: ("garder", None, 0.9, "document utile") for x in kept_names})
Local.asked.clear()
claude_asked()
before = len(rplan()["actions"])
for _ in range(2):
    p = rrun("--decide", FILE_SORTER_CLEANUP="on")
asked_names = {n for x in Local.asked for n in x["names"]} | {n for x in claude_asked() for n in x}
check("review, on: a small folder read again for a new document, without what waits for the Mac nor what the server already moved",
      p.returncode == 0 and asked_names == set(kept_names) and len(rplan()["actions"]) == before)
lagging.unlink()
plan = rplan()
kept_one = "60 Achats/Commandes/2026-10-02 Boutique - Facture.txt"
photo, receipt = "80 Images/Photos/IMG_0001.jpg", "10 Identité & Banque/Identité/Récépissé.txt"
plan["actions"] += [{**plan["actions"][0], "id": "bad-source", "source": {"racine": "icloud", "chemin": "00 À trier/x.txt"}},
                    {"id": "bad-target", "type": "archiver", "source": {"racine": "icloud", "chemin": kept_one}, "taille": (icloud2 / kept_one).stat().st_size,
                     "cible": {"racine": "icloud", "chemin": "30 Santé/x.txt"}, "cree_le": time.time()},
                    {"id": "bad-photos", "type": "supprimer", "source": {"racine": "icloud", "chemin": photo}, "taille": (icloud2 / photo).stat().st_size,
                     "cree_le": time.time()},
                    {"id": "kept-gone", "type": "supprimer", "source": {"racine": "icloud", "chemin": receipt}, "taille": (icloud2 / receipt).stat().st_size,
                     "garde": {"racine": "icloud", "chemin": "10 Identité & Banque/Identité/Absent.txt"}, "cree_le": time.time()},
                    {"id": "drive-trash", "type": "supprimer", "source": {"racine": "drive", "chemin": "50 Travail/Contrats/2026-01-01 Client - Contrat.txt"},
                     "taille": (contrats / "2026-01-01 Client - Contrat.txt").stat().st_size, "cree_le": time.time()}]
(data2 / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
p = rrun("--apply")
done = json.loads((data2 / "results.json").read_text(encoding="utf-8"))["appliques"]
trash = sorted(f.name for f in (home / ".Trash").iterdir()) if (home / ".Trash").exists() else []
check("Mac: the review's archives moved into iCloud's archive, at the same path", p.returncode == 0
      and (icloud2 / "90 Archives/10 Identité & Banque/Identité/2015-06-01 Préfecture - Carte d'identité.txt").exists()
      and len(list((icloud2 / "90 Archives/50 Travail/Factures 2019").iterdir())) == 12 and not (ident / "2015-06-01 Préfecture - Carte d'identité.txt").exists())
check("Mac: the review's deletions in the Trash, what is kept still there", trash == sorted(["2024-05-02 Préfecture - Carte d'identité (2).txt", "CGV Boutique.txt",
                                                                                            "CNI recto flou.jpg", "Permis B.jpg", "Pré-demande ANTS.txt"])
      and all((ident / x).exists() for x in ("2024-05-02 Préfecture - Carte d'identité.txt", "CNI recto.jpg", "Permis A.jpg", "Récépissé.txt")))
check("Mac: a folder left empty goes (Ancien bail), a theme's first sub-folders stay even empty (Factures 2019, Logement)",
      not bail.exists() and factures.is_dir() and (icloud2 / "20 Administratif" / "Logement").is_dir())
check("Mac: refuses a review action out of a theme folder, or archiving anywhere but the archive",
      done["bad-source"] == "erreur : ValueError source hors des dossiers par thèmes" and done["bad-target"] == "erreur : ValueError destination hors des archives"
      and (icloud2 / kept_one).exists() and not (icloud2 / "30 Santé" / "x.txt").exists())
check("Mac: refuses the photos, and a copy whose kept one is no longer there", (icloud2 / photo).exists() and (icloud2 / receipt).exists()
      and done["bad-photos"] == "erreur : ValueError source dans un dossier que la relecture ne touche pas"
      and done["kept-gone"] == "erreur : ValueError l'exemplaire gardé à sa place n'est plus là")
check("Mac: never the Drive's trash, even when a plan asks for it", done["drive-trash"] == "erreur : ValueError rien n'est mis à la corbeille dans le Drive"
      and (contrats / "2026-01-01 Client - Contrat.txt").exists())
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
new = revents()[n:]
check("review, on: what the Mac did comes back, « archivé par le Mac » and « supprimé par le Mac », with the reason",
      by(new, "archivé par le Mac", "2018-07-01 Agence - Bail.txt")["to"] == "iCloud/90 Archives/20 Administratif/Logement/Ancien bail" and by(new, "supprimé par le Mac", "CGV Boutique.txt")["detail"] == "conditions générales de vente sans intérêt"
      and sum(x["action"] == "archivé par le Mac" for x in new) == 14 and sum(x["action"] == "supprimé par le Mac" for x in new) == 5)
st = rstate()
check("review, on: nothing left waiting in review.json", not [e for e in st["files"].values() if e.get("state") == "pending"])

# a new document in a small folder: read again with the others; the models out of reach: the review waits an hour, the inboxes do not
write(ident / "Carte identité scan.txt", "Carte nationale d'identité délivrée le 02/05/2024, scan. ")
os.utime(ident / "Carte identité scan.txt", (old, old))
st["rest_until"] = 0
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
Local.down = True
Local.asked.clear()
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="on", FAKE_CLAUDE_FAIL="1")
Local.down = False
check("review: the models out of reach, the review waits an hour, the step says why without a file name, the pass goes through", p.returncode == 0
      and rreport()["steps"]["decide"] == "failed: relecture des dossiers en pause une heure : limite d'usage de Claude atteinte, reprise vers 21:30"
      and rstate()["pause_until"] > time.time() + 3000 and not [x for x in revents()[n:] if x["action"] in ("revu", "échec")]
      and [(e["state"], e["tries"]) for k, e in rstate()["files"].items() if "Carte identité scan" in k] == [("unanswered", 0)])   # no fault of its own
claude_asked()
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
check("review: paused, no question at all", p.returncode == 0 and not Local.asked and not claude_asked())
st = rstate()
st["pause_until"] = 0   # an hour later
for e in st["files"].values():
    e["time"] -= 3600 if e.get("state") == "unanswered" else 0
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
review_answers({**{x: ("garder", None, 0.9, "document utile") for x in kept_names},
                "Carte identité scan.txt": ("supprimer", "2024-05-02 Préfecture - Carte d'identité.txt", 0.95, "même carte, scan moins net")})
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
new = revents()[n:]
check("review: a new document in a small folder is read with all the others, and a lesser copy of one of them goes",
      p.returncode == 0 and [x["detail"] for x in new if x["action"] == "revu"] == ["7 document(s) relu(s) : 6 gardé(s), 0 archivé(s), 1 supprimé(s)"]
      and by(new, "supprimé", "Carte identité scan.txt")["detail"].startswith("copie moins bonne de « 2024-05-02 Préfecture - Carte d'identité.txt »"))

# the filing of the inboxes: the archive is not offered as a destination, and a pass with files to decide leaves the review aside
write(icloud2 / "00 À trier" / "Ordonnance.txt", "Ordonnance du docteur Martin, 04/11/2026. ")
answer({"Ordonnance.txt": ("30 Santé", "2026-11-04 Dr Martin - Ordonnance.txt", 0.9, "ordonnance")})
st = rstate()
st["rest_until"] = 0
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
Local.asked.clear()
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
tree = Local.asked[0]["body"]["messages"][0]["content"].split("Règles :")[0] if Local.asked else ""
check("filing: the archive is not offered as a destination; the review waits while the inboxes need the models",
      p.returncode == 0 and "- 10 Identité & Banque/Identité" in tree and "90 Archives" not in tree and len(Local.asked) == 1
      and not [x for x in revents()[n:] if x["action"] == "revu"])
# iCloud's mount down (an empty folder): the review forgets nothing of iCloud and carries nothing out there
kept_before = {k for k in rstate()["files"] if k.startswith("icloud|")}
st = rstate()
st["rest_until"] = 0
st["files"]["icloud|10 Identité & Banque/Identité/Fantôme.txt|99"] = {"path": "10 Identité & Banque/Identité/Fantôme.txt", "decision": "archiver",
                                                                     "state": "pending", "time": time.time(), "reason": "test"}
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
(R / "icloud-down").mkdir()   # a mount point with nothing mounted on it
p = rrun("--decide", FILE_SORTER_CLEANUP="on", FILE_SORTER_ICLOUD=str(R / "icloud-down"))
st = rstate()
check("review, iCloud's mount down: nothing forgotten, nothing carried out", p.returncode == 0 and kept_before <= set(st["files"])
      and st["files"]["icloud|10 Identité & Banque/Identité/Fantôme.txt|99"]["state"] == "pending")
del st["files"]["icloud|10 Identité & Banque/Identité/Fantôme.txt|99"]
st["rest_until"] = 0   # that pass ended a walk (over the Drive alone)
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

# FILE_SORTER_CLEANUP=off: no review at all; any value but on and off: the trial, where nothing moves
write(achats / "Bon de commande.txt", "Bon de commande de 2019 pour un article livré. ")
os.utime(achats / "Bon de commande.txt", (old, old))
review_answers({"Bon de commande.txt": ("archiver", None, 0.9, "commande livrée en 2019"), "2026-10-02 Boutique - Facture.txt": ("garder", None, 0.9, "facture récente")})
Local.asked.clear()
claude_asked()
n = len(revents())
pending_mac = [a["source"]["chemin"].rsplit("/", 1)[1] for a in rplan()["actions"] if a["type"] in ("archiver", "supprimer")]
late = next(a for a in rplan()["actions"] if a["type"] == "supprimer")
p = rrun("--decide", FILE_SORTER_CLEANUP="off")
check("review off: no question; what the Mac had still to do leaves its plan, « annulé » in the journal",
      p.returncode == 0 and not Local.asked and not claude_asked() and pending_mac == ["Carte identité scan.txt"]
      and [(x["action"], x["object"]) for x in revents()[n:]] == [("annulé", "Carte identité scan.txt")]
      and not [a for a in rplan()["actions"] if a["type"] in ("archiver", "supprimer")])
results = json.loads((data2 / "results.json").read_text(encoding="utf-8"))
results["appliques"][late["id"]] = "ok"   # the Mac had fetched the plan before, and trashed it anyway
(data2 / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="off")
check("review off: what the Mac did anyway with a withdrawn action is still logged, once",
      [(x["action"], x["object"]) for x in revents()[n:]] == [("supprimé par le Mac", "Carte identité scan.txt")] and not rstate()["withdrawn"])
p = rrun("--decide", FILE_SORTER_CLEANUP="off")
check("review off: and not twice", len(revents()) == n + 1)
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="yes")
new = revents()[n:]
check("review, an unknown FILE_SORTER_CLEANUP: the trial, a proposal only", p.returncode == 0 and by(new, "à archiver", "Bon de commande.txt")
      and not by(new, "archivé", "Bon de commande.txt") and (achats / "Bon de commande.txt").exists()
      and not [a for a in rplan()["actions"] if a["source"]["chemin"].endswith("Bon de commande.txt")])
# names that only differ by punctuation are never taken for each other; a copy named only in its reason; a copy that names none
billets = icloud2 / "70 Sport & Loisirs" / "Billets"
billets.mkdir(parents=True)
for i, name in enumerate(("Scan 1.jpg", "Scan-1.jpg", "Permis C.jpg", "Permis D.jpg", "Photo floue.jpg", "Permis E.jpg", "Permis F.jpg")):
    (billets / name).write_bytes(b"\xff\xd8\xff\xe0 billet " + bytes([i + 10]) * 60)
    os.utime(billets / name, (old, old))
review_answers({"Scan 1.jpg": ("supprimer", None, 0.95, "billet périmé"), "Scan-1.jpg": ("garder", None, 0.9, "billet à venir"),
                "Permis C.jpg": ("supprimer", None, 0.95, "copie de Permis D.jpg, moins nette"), "Permis D.jpg": ("supprimer", None, 0.95, "même permis que Permis C.jpg"),
                "Photo floue.jpg": ("supprimer", None, 0.95, "copie floue d'un autre document"),
                "Permis E.jpg": ("supprimer", None, 0.95, "scan flou de Permis F.jpg, inutile"), "Permis F.jpg": ("supprimer", None, 0.95, "photo ratée de Permis E.jpg, inutile")})
rows = json.loads(answers.read_text(encoding="utf-8"))
rows["Scan 1.jpg"]["fichier"] = "scan 1.JPG"   # the model writes it back loosely: « Scan 1.jpg » or « Scan-1.jpg »?
answers.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
st = rstate()
st.update(rest_until=0, pause_until=0, cursor=None)
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="yes")
new = revents()[n:]
st = rstate()["files"]
check("review: an answer that could name two files names none; the other one keeps its own answer", p.returncode == 0
      and st["icloud|70 Sport & Loisirs/Billets/Scan 1.jpg|" + str((billets / "Scan 1.jpg").stat().st_size)]["state"] == "unanswered"
      and st["icloud|70 Sport & Loisirs/Billets/Scan-1.jpg|" + str((billets / "Scan-1.jpg").stat().st_size)]["decision"] == "garder")
check("review: copies named only in their reasons, one stays, copy wording or not; a copy that names nothing stays",
      [x["object"] for x in new if x["action"] == "à supprimer"] == ["Permis D.jpg", "Permis F.jpg"]
      and by(new, "à supprimer", "Permis D.jpg")["detail"] == "essai, rien n'a bougé : copie moins bonne de « Permis C.jpg » : même permis que Permis C.jpg")

(billets / "Billet concert.jpg").write_bytes(b"\xff\xd8\xff\xe0 concert " + bytes([30]) * 60)   # a new document: the small folder read again whole
os.utime(billets / "Billet concert.jpg", (old, old))
rows["Billet concert.jpg"] = {"raison": "concert à venir", "decision": "garder", "doublon_de": None, "confiance": 0.9}
answers.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
st = rstate()
st.update(rest_until=0, cursor=None)
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
claude_asked()
n = len(revents())
p = rrun("--decide", FILE_SORTER_CLEANUP="yes")
new = revents()[n:]
check("review, trial: a folder read again for a new document does not repeat a proposal already in the journal", p.returncode == 0
      and [x["action"] for x in new] == ["revu"] and "Permis D.jpg" in claude_asked()[0])

# a folder of more than ten documents, ten or fewer once those going to the archive are set aside: a new one is read with those that stay
cartes = icloud2 / "70 Sport & Loisirs" / "Cartes"
cartes.mkdir(parents=True)
olds, keeps = [f"Ancienne carte {i}.txt" for i in range(6)], [f"Carte {c}.txt" for c in "ABCDE"]
for name in olds + keeps:
    write(cartes / name, f"Carte de membre du club, {name}, saison passée ou en cours. ")
    os.utime(cartes / name, (old, old))
review_answers({**{x: ("archiver", None, 0.9, "carte d'une saison passée") for x in olds}, **{x: ("garder", None, 0.9, "carte en cours") for x in keeps},
                "Nouvelle carte.txt": ("garder", None, 0.9, "nouvelle carte")})
st = rstate()
st.update(rest_until=0, pause_until=0, cursor=None)
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
for _ in range(2):   # eleven documents: ten, then one
    p = rrun("--decide", FILE_SORTER_CLEANUP="yes")
write(cartes / "Nouvelle carte.txt", "Nouvelle carte de membre du club, saison 2026. ")
os.utime(cartes / "Nouvelle carte.txt", (old, old))
st = rstate()
st.update(rest_until=0, pause_until=0, cursor=None)
(data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
Local.asked.clear()
p = rrun("--decide", FILE_SORTER_CLEANUP="yes")
asked = {n for x in Local.asked for n in x["names"]}
content = "".join(x["body"]["messages"][0]["content"] for x in Local.asked)
check("review: a new document read with those that stay, the folder whole once those going to the archive are set aside, these not listed",
      p.returncode == 0 and asked == set(keeps) | {"Nouvelle carte.txt"} and "- Carte A.txt" in content and "Ancienne carte" not in content
      and sum(e.get("state") == "pending" for k, e in rstate()["files"].items() if "/Cartes/" in k) == 6)

matchs = icloud2 / "70 Sport & Loisirs" / "Matchs"
matchs.mkdir(parents=True)
for i, name in enumerate(("Bad.jpg", "Good.jpg")):
    (matchs / name).write_bytes(b"\xff\xd8\xff\xe0 match " + bytes([i + 40]) * 60)
    os.utime(matchs / name, (old, old))
rows = {"Good.jpg": {"raison": "billet du prochain match", "decision": "garder", "doublon_de": None, "confiance": 0.9}}
answers.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def later_pass(hours):
    """A pass of the review some hours after the last one (its pause over, its documents without an answer due again)."""
    st = rstate()
    st.update(rest_until=0, pause_until=0, cursor=None)
    for e in st["files"].values():
        if e["path"].startswith("70 Sport & Loisirs/Matchs/") and e.get("state") == "unanswered":
            e["time"] -= hours * 3600
    (data2 / "review.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    claude_asked()
    p = rrun("--decide", FILE_SORTER_CLEANUP="yes", FAKE_CLAUDE_FAIL_ON="Bad.jpg")
    return p, claude_asked()


tries = lambda name: next((e.get("state"), e.get("tries"), e.get("decision")) for e in rstate()["files"].values() if e["path"].endswith("/" + name))
asked = [later_pass(h)[1] for h in (0, 1, 24, 24)]
check("review: a document Claude always refuses is asked alone after two tries, and the one asked with it is read at last",
      asked == [[["Bad.jpg", "Good.jpg"]], [["Bad.jpg", "Good.jpg"]], [["Bad.jpg"]], [["Good.jpg"]]]
      and tries("Bad.jpg") == ("unanswered", 3, None) and tries("Good.jpg")[0] == "kept")

(data2 / "review.json").write_text("[1, 2]", encoding="utf-8")   # a broken state: the review starts again, the inboxes go on
write(icloud2 / "00 À trier" / "Lettre.txt", "Lettre de la mairie, 05/11/2026. ")
answer({"Lettre.txt": ("20 Administratif/Logement", "2026-11-05 Mairie - Lettre.txt", 0.9, "lettre")})
p = rrun("--decide", FILE_SORTER_CLEANUP="on")
check("review: a broken review.json never stops the pass: the inboxes are filed, the state starts again", p.returncode == 0
      and "review.json unreadable" in p.stdout and isinstance(rstate(), dict)
      and any(a["source"]["chemin"] == "00 À trier/Lettre.txt" for a in rplan()["actions"]))
check("review: no file left behind by the atomic writes", not list(data2.glob("*.tmp")) and not list(data2.glob(".*.tmp")))

# ---- the rules of 05/11/2026 evening (ticket 14): from 0.7 a document the model wants out leaves its folder, to the trash from 0.9 and
# into the archive below; a document modified in the last three hours waits; a folder where a document stayed under the former rules is
# read again whole ----
N = T / "rules"
icloud4, drive4, data4 = N / "icloud", N / "drive", N / "data"
banque, ident4 = icloud4 / "10 Identité & Banque" / "Banque", icloud4 / "10 Identité & Banque" / "Identité"
for d in (data4, icloud4 / "00 À trier", drive4 / "00 À trier", banque, ident4):
    d.mkdir(parents=True, exist_ok=True)
write(banque / "2013-11-27 Banque - RIB.txt", "Relevé d'identité bancaire du compte courant, 2013. ")
write(banque / "2024-01-10 Banque - RIB.txt", "Relevé d'identité bancaire du compte courant, 2024. ")
write(banque / "2019-01-07 Banque - Relevé.txt", "Relevé du compte courant, janvier 2019. ")
for i, name in enumerate(("2018 CNI.png", "2018 CNI recto.jpeg", "2026 CNI.jpg", "2026 CNI scan.jpg")):
    (ident4 / name).write_bytes(b"\xff\xd8\xff\xe0 carte " + bytes([i + 60]) * 60)   # no text: Claude reads them
(ident4 / "2018 CNI (2).pdf").write_bytes(b"%PDF-1.4 carte " + bytes(range(64)))
write(ident4 / "Notice.txt", "Notice d'utilisation d'un service, sans rapport avec le propriétaire. ")
write(ident4 / "Brouillon.txt", "Un brouillon de lettre pour la préfecture. ")
write(ident4 / "Attestation.txt", "Attestation de domicile, 2026. ")
write(ident4 / "Récent.txt", "Un document enregistré il y a une heure. ")
for f in N.rglob("*"):
    if f.is_file():
        os.utime(f, (old, old))
for name, hours in (("Attestation.txt", 4), ("Récent.txt", 1)):
    os.utime(ident4 / name, (time.time() - hours * 3600,) * 2)
rel = lambda f: f"icloud|{f.relative_to(icloud4)}|{f.stat().st_size}"
former = lambda f, decision, c, state: {"path": str(f.relative_to(icloud4)), "decision": decision, "confidence": c, "reason": "décision d'avant",
                                        "twin": None, "engine": "local", "model": "qwen", "time": time.time(), "state": state}
(data4 / "review.json").write_text(json.dumps({   # Banque read this afternoon under the former rules: an old RIB stayed, not sure enough
    "files": {rel(banque / "2013-11-27 Banque - RIB.txt"): former(banque / "2013-11-27 Banque - RIB.txt", "garder", 0.75, "kept"),
              rel(banque / "2024-01-10 Banque - RIB.txt"): former(banque / "2024-01-10 Banque - RIB.txt", "garder", 0.95, "kept"),
              rel(banque / "2019-01-07 Banque - Relevé.txt"): former(banque / "2019-01-07 Banque - Relevé.txt", "archiver", 0.95, "pending")},
    "cursor": [0, "10 Identité & Banque", "Banque"], "sweep": {"files": 3, "archiver": 1}}, ensure_ascii=False), encoding="utf-8")
review_answers({"2013-11-27 Banque - RIB.txt": ("supprimer", "2024-01-10 Banque - RIB.txt", 0.75, "ancien RIB, remplacé par celui de 2024"),
                "2024-01-10 Banque - RIB.txt": ("garder", None, 0.95, "RIB du compte en cours"),
                "2019-01-07 Banque - Relevé.txt": ("archiver", None, 0.95, "relevé de 2019"),
                "2018 CNI.png": ("archiver", None, 0.8, "carte de 2018, remplacée par celle de 2026"),
                "2018 CNI recto.jpeg": ("supprimer", "2018 CNI.png", 0.75, "recto seul, moins bon que le PNG"),
                "2018 CNI (2).pdf": ("archiver", None, 0.7, "autre photo de la carte de 2018"),
                "2026 CNI.jpg": ("supprimer", "2026 CNI scan.jpg", 0.8, "même carte que le scan"),
                "2026 CNI scan.jpg": ("supprimer", "2026 CNI.jpg", 0.8, "même carte que la photo"),
                "Notice.txt": ("supprimer", None, 0.95, "notice sans intérêt"),
                "Brouillon.txt": ("archiver", None, 0.65, "peut-être un ancien brouillon"),
                "Attestation.txt": ("garder", None, 0.9, "attestation récente"),
                "Récent.txt": ("supprimer", None, 0.99, "jamais demandé")})
nenv = {"FILE_SORTER_ICLOUD": str(icloud4), "FILE_SORTER_DRIVE": str(drive4), "FILE_SORTER_DATA": str(data4), "FAKE_DRIVE": str(drive4)}
nevents = lambda: [json.loads(x) for x in (data4 / "activity.jsonl").read_text(encoding="utf-8").splitlines()] if (data4 / "activity.jsonl").exists() else []
nstate = lambda: json.loads((data4 / "review.json").read_text(encoding="utf-8"))
Local.asked.clear()
claude_asked()
p = run("--decide", **nenv)
ev = nevents()
check("rules: under new rules, a folder where a document stayed for lack of certainty is read again whole at once, the walk starting over",
      p.returncode == 0 and "review: new rules, 1 folder(s) read again (3 document(s))" in p.stdout and nstate()["rules"] == 3
      and [sorted(x["names"]) for x in Local.asked] == [sorted(["2013-11-27 Banque - RIB.txt", "2024-01-10 Banque - RIB.txt", "2019-01-07 Banque - Relevé.txt"])]
      or print(p.stdout[-3000:], p.stderr[-3000:]))
check("rules: a lesser copy wanted in the trash at 0.75 goes to the archive, saying why; a proposal already in the journal is not repeated",
      [(x["action"], x["object"]) for x in ev] == [("revu", "iCloud/10 Identité & Banque/Banque"), ("à archiver", "2013-11-27 Banque - RIB.txt")]
      and ev[0]["detail"] == "3 document(s) relu(s) : 1 gardé(s), 2 à archiver, 0 à supprimer ; essai : rien n'a bougé"
      and ev[1]["detail"] == "essai, rien n'a bougé : copie moins bonne de « 2024-01-10 Banque - RIB.txt » : ancien RIB, remplacé par celui de 2024 ; "
                             "à l'archive plutôt qu'à la corbeille, faute de certitude" and ev[1]["to"] == "iCloud/90 Archives/10 Identité & Banque/Banque")
Local.asked.clear()
p = run("--decide", **nenv)
new = nevents()[len(ev):]
asked_names = {n for x in Local.asked for n in x["names"]} | {n for x in claude_asked() for n in x}
check("rules: a document of more than three hours is read, one of the last hour only listed",
      p.returncode == 0 and "Attestation.txt" in asked_names and "Récent.txt" not in asked_names
      and any("- Récent.txt (1 Ko)" in x["body"]["messages"][0]["content"] for x in Local.asked))
check("rules: from 0.7 out of the folder; the copy of a card going to the archive follows it there, the trash only from 0.9, below 0.7 it stays",
      [x["detail"] for x in new if x["action"] == "revu"] == ["8 document(s) relu(s) : 3 gardé(s), 4 à archiver, 1 à supprimer ; essai : rien n'a bougé"]
      and sorted(x["object"] for x in new if x["action"] == "à archiver") == ["2018 CNI (2).pdf", "2018 CNI recto.jpeg", "2018 CNI.png", "2026 CNI.jpg"]
      and [x["object"] for x in new if x["action"] == "à supprimer"] == ["Notice.txt"]
      and by(new, "à archiver", "2018 CNI recto.jpeg")["detail"].startswith("essai, rien n'a bougé : copie moins bonne de « 2018 CNI.png » : recto seul"))
st = nstate()["files"]
check("rules: of two copies naming each other one stays, also when the other goes to the archive; the model's own decision is kept",
      st[rel(ident4 / "2026 CNI scan.jpg")]["decision"] == "garder" and st[rel(ident4 / "2026 CNI.jpg")]["decision"] == "archiver"
      and st[rel(ident4 / "2018 CNI recto.jpeg")]["proposal"] == "supprimer" and st[rel(ident4 / "Brouillon.txt")]["decision"] == "garder")
sent = next(x for x in Local.asked if "### Notice.txt" in x["body"]["messages"][0]["content"])["body"]["messages"][0]["content"]
check("rules: the models' scale stays the same, without telling them what the script does with it",
      "Si tu n'es pas sûr, mets moins de 0.8." in sent and "restera en place" not in sent)
(T / "notified.txt").unlink(missing_ok=True)
p = run("--decide", **nenv)
notified = (T / "notified.txt").read_text(encoding="utf-8").splitlines() if (T / "notified.txt").exists() else []
check("rules: the end of the trial's walk counts each document once, read again or not",
      p.returncode == 0 and notified == ["Tri des fichiers Relecture d'essai des dossiers terminée : 11 documents relus, 6 à archiver et 1 à supprimer proposés, "
                                         "rien n'a bougé. Le détail est dans le journal d'activité"])
p = run("--decide", FILE_SORTER_CLEANUP="on", **nenv)
kinds = {a["source"]["chemin"].rsplit("/", 1)[1]: a["type"] for a in json.loads((data4 / "plan.json").read_text(encoding="utf-8"))["actions"]}
check("rules, on: what was not sure enough for the trash is archived by the Mac, the rest trashed as before", p.returncode == 0
      and kinds == {"2013-11-27 Banque - RIB.txt": "archiver", "2019-01-07 Banque - Relevé.txt": "archiver", "2018 CNI.png": "archiver",
                    "2018 CNI recto.jpeg": "archiver", "2018 CNI (2).pdf": "archiver", "2026 CNI.jpg": "archiver", "Notice.txt": "supprimer"})

# ---- the rules of 06/11/2026: only official documents still current stay; every document kept is asked again, several folders a pass ----
O = T / "official"
icloud5, drive5, data5 = O / "icloud", O / "drive", O / "data"
permis5, factures5 = icloud5 / "10 Identité & Banque" / "Permis de conduire", icloud5 / "60 Finances" / "Factures & Reçus"
for d in (data5, icloud5 / "00 À trier", drive5 / "00 À trier", drive5 / "50 Travail", permis5, factures5):
    d.mkdir(parents=True, exist_ok=True)
docs5 = {permis5 / "La Poste - Avis de réception recommandé.txt": ("garder", 0.98, "kept", "local"),
         permis5 / "France Stage Permis - Reçu de paiement.txt": ("garder", 0.99, "kept", "local"),
         permis5 / "Préfecture - Reconstitution de points.txt": ("garder", 0.99, "kept", "local"),
         permis5 / "Ancien permis.txt": ("archiver", 0.9, "pending", "local"),   # already leaving: neither asked again nor listed
         factures5 / "Cora - Ticket de caisse.txt": ("garder", 0.95, "kept", "local"),
         factures5 / "Vieux tableur.xls": ("garder", 0.0, "kept", None)}   # a format the review does not read: no model asked
for f in docs5:
    write(f, f"Document {f.stem}, à relire. ")
    os.utime(f, (old, old))
entry5 = lambda f, dec, c, state, engine: {"path": str(f.relative_to(icloud5)), "decision": dec, "confidence": c, "reason": "règles d'avant", "twin": None,
                                           **({"engine": engine, "model": "qwen"} if engine else {}), "time": time.time(), "state": state}
(data5 / "review.json").write_text(json.dumps({"files": {f"icloud|{f.relative_to(icloud5)}|{f.stat().st_size}": entry5(f, *v) for f, v in docs5.items()},
                                               "rules": 2, "cursor": None, "rest_until": time.time() + 3600, "first_sweep": time.time(), "bilan": "2026-11-06"},
                                              ensure_ascii=False), encoding="utf-8")
review_answers({"La Poste - Avis de réception recommandé.txt": ("supprimer", None, 0.95, "avis de réception, pas un document officiel"),
                "France Stage Permis - Reçu de paiement.txt": ("supprimer", None, 0.95, "reçu de paiement, pas un document officiel"),
                "Préfecture - Reconstitution de points.txt": ("garder", None, 0.95, "décision de la préfecture"),
                "Cora - Ticket de caisse.txt": ("supprimer", None, 0.95, "ticket de caisse")})
Local.asked.clear()
p = run("--decide", FILE_SORTER_REVIEW_PASS="30", FILE_SORTER_ICLOUD=str(icloud5), FILE_SORTER_DRIVE=str(drive5), FILE_SORTER_DATA=str(data5), FAKE_DRIVE=str(drive5))
asked = {n for x in Local.asked for n in x["names"]}
oev = [json.loads(x) for x in (data5 / "activity.jsonl").read_text(encoding="utf-8").splitlines()] if (data5 / "activity.jsonl").exists() else []
check("rules of 06/11: every document a model kept is asked again at once, the walk starting over; not what leaves, nor what cannot be read",
      p.returncode == 0 and "review: new rules, 2 folder(s) read again (4 document(s))" in p.stdout
      and json.loads((data5 / "review.json").read_text(encoding="utf-8"))["rules"] == 3
      and asked == {"La Poste - Avis de réception recommandé.txt", "France Stage Permis - Reçu de paiement.txt", "Préfecture - Reconstitution de points.txt",
                    "Cora - Ticket de caisse.txt"}
      or print(p.stdout[-3000:], p.stderr[-2000:]))
check("rules of 06/11: several folders read in one pass, each in its « revu »; the receipts and the acknowledgement « à supprimer »",
      [x["object"] for x in oev if x["action"] == "revu"] == ["iCloud/10 Identité & Banque/Permis de conduire", "iCloud/60 Finances/Factures & Reçus"]
      and sorted(x["object"] for x in oev if x["action"] == "à supprimer") == ["Cora - Ticket de caisse.txt", "France Stage Permis - Reçu de paiement.txt",
                                                                               "La Poste - Avis de réception recommandé.txt"])
check("rules of 06/11: the trial's walk over, its bilan in the journal at once", "BILAN   end: 6 document(s), 1 kept, 1 to archive, 3 to trash, 1 unreadable" in p.stdout)
sent = next(x for x in Local.asked if "### Cora - Ticket de caisse.txt" in x["body"]["messages"][0]["content"])["body"]["messages"][0]["content"]
check("rules of 06/11: the question keeps only official documents still current, a receipt or an acknowledgement is none, in doubt the archive",
      '"garder" : SEULEMENT un document officiel encore d\'actualité' in sent and 'Dans le doute entre "garder" et "archiver" : archiver' in sent
      and "Un reçu, une confirmation, un accusé ou un avis de réception, une preuve de dépôt ou un suivi n'est pas un document officiel" in sent
      and "Ancien permis" not in "".join(x["body"]["messages"][0]["content"] for x in Local.asked))

# ---- what the iPhone saves into the Drive without a folder: Chrome's « Enregistré depuis Chrome » and the Drive's root, sorted too ----
C = T / "chrome"
icloud3, drive3, data3 = C / "icloud", C / "drive", C / "data"
chrome = drive3 / unicodedata.normalize("NFD", "Enregistré depuis Chrome")   # as a mount may show it: « é » decomposed
for d in (data3, icloud3 / "00 À trier", icloud3 / "30 Santé", icloud3 / "50 Travail", drive3 / "00 À trier", chrome):   # the tree is iCloud's
    d.mkdir(parents=True, exist_ok=True)
(drive3 / "Réservation-Rendez-vous-confirmé.pdf").write_bytes(b"%PDF-1.4 reservation " + bytes(range(60)))   # no text: Claude reads it
write(drive3 / "notes.txt", "Des notes qu'un programme garde à la racine du Drive. ")   # not a document of the root: left alone
(chrome / "Billet.pdf").write_bytes(b"%PDF-1.4 billet " + bytes(range(70)))
(chrome / "Devis.docx").write_bytes(b"PK not really a docx " + bytes(range(80)))
(chrome / "setup.zip").write_bytes(b"PK zip " + bytes(range(90)))   # not a document: left alone
answer({"Réservation-Rendez-vous-confirmé.pdf": ("30 Santé", "2026-11-05 Clinique - Réservation rendez-vous.pdf", 0.9, "confirmation de rendez-vous"),
        "Billet.pdf": ("30 Santé", "Billet.pdf", 0.4, "billet illisible"),
        "Devis.docx": ("50 Travail", "2026-11-05 Artisan - Devis.docx", 0.9, "devis de travaux")})
prompts = C / "prompts.jsonl"
cenv = {"FILE_SORTER_ICLOUD": str(icloud3), "FILE_SORTER_DRIVE": str(drive3), "FILE_SORTER_DATA": str(data3), "FAKE_DRIVE": str(drive3),
        "FAKE_CLAUDE_PROMPTS": str(prompts)}
claude_asked()
p = run("--decide", **cenv)
cev = [json.loads(x) for x in (data3 / "activity.jsonl").read_text(encoding="utf-8").splitlines()]
e = next((x for x in cev if x["action"] == "rangé" and x["object"] == "Réservation-Rendez-vous-confirmé.pdf"), None)
check("Drive's root: a document saved there is filed like an inbox's, into iCloud by the Mac", p.returncode == 0 and e
      and e["from"] == "Google Drive" and e["to"] == "iCloud/30 Santé/2026-11-05 Clinique - Réservation rendez-vous.pdf" and "le Mac le range" in e["detail"]
      or print(p.stdout[-2000:], p.stderr[-2000:]))
e = next((x for x in cev if x["action"] == "rangé" and x["object"] == "Devis.docx"), None)
check("Chrome's folder: an editable document filed by the server in the Drive", e and e["from"] == "Google Drive/Enregistré depuis Chrome"
      and e["to"] == "Google Drive/50 Travail/2026-11-05 Artisan - Devis.docx" and (drive3 / "50 Travail" / "2026-11-05 Artisan - Devis.docx").exists())
e = next((x for x in cev if x["action"] == "à trier" and x["object"] == "Billet.pdf"), None)
check("Chrome's folder: a doubtful download joins the Drive's 00 À trier", e and e["to"] == "Google Drive/00 À trier/Billet.pdf"
      and (drive3 / "00 À trier" / "Billet.pdf").exists() and not (chrome / "Billet.pdf").exists())
asked = {n for x in claude_asked() for n in x}
question = [json.loads(x) for x in prompts.read_text(encoding="utf-8").splitlines()]
check("Drive's root and Chrome's folder: what is not a document stays, never asked about", (drive3 / "notes.txt").exists() and (chrome / "setup.zip").exists()
      and not asked & {"notes.txt", "setup.zip"} and "Réservation-Rendez-vous-confirmé.pdf" in asked)
check("Drive's root: Claude reads a copy, never the whole Drive", question and str(drive3) not in question[0]["dirs"]
      and str(chrome) in question[0]["dirs"] and f"{drive3}/Réservation-Rendez-vous-confirmé.pdf" not in question[0]["prompt"])
p = run("--apply", **cenv)
check("Mac: takes a document of the Drive's root into iCloud", p.returncode == 0 and (icloud3 / "30 Santé" / "2026-11-05 Clinique - Réservation rendez-vous.pdf").exists()
      and not (drive3 / "Réservation-Rendez-vous-confirmé.pdf").exists())
shutil.rmtree(T)
LOCAL.shutdown()

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
