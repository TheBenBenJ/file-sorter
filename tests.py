#!/usr/bin/env python3
"""Checks of file-sorter, offline and without touching anything real: python3 -I tests.py
The server's pass (sort.py --decide) runs in a sandbox: a copy of the code with the example settings, fake iCloud and Drive folders, a fake
claude that answers from a table, a fake local model (the home server's OpenAI API, its /tokenize included) that answers from another,
a fake rclone that moves inside the fake Drive. Checked: the activity journal and the step report the Dashboard reads (activity.py), pass
after pass: decisions, moves in the Drive, the Mac's results, a failing model, a failing Drive; the shadow mode of the server's unit
(FILE_SORTER_SHADOW=1: Claude decides, the local model is compared), then the local model deciding for the files with text, its batches
measured against its 16k-token slot, and the local model down.
The French file names and texts below are test data, as the owner's would be."""
import contextlib, http.server, io, json, os, re, shutil, subprocess, sys, tempfile, threading
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
for quiet in ("notifier", "osascript"):
    (fake / quiet).write_text("#!/bin/sh\nexit 0\n")
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
         "FILE_SORTER_LOCAL_URL": f"http://127.0.0.1:{LOCAL.server_port}", "FILE_SORTER_SHADOW": "1",
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
shutil.rmtree(T)
LOCAL.shutdown()

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
