"""Activity journal and step report of the file sorting, read by the Dashboard's System tab (convention of the home server: its
docs/architecture.md, « Activity journal » and « Step report »). Only `sort.py --decide`, on the server, writes them: the server is their
single writer, and what the Mac does comes back in results.json, logged at the server's next pass. Elsewhere PATH stays None.

Journal (activity.jsonl in the data folder): one JSON event per line, appended (JSONL, UTF-8, one write per event): {"time", "action",
"object"} plus, when not empty, "from", "to", "detail", "engine", "model", "confidence" and "run". object, from and to are cut at 300
characters, detail at 1,000. The texts are French (the owner reads them) and may hold file names: the file stays private (mode 600) and
never feeds a notification. Past 5 MB it is rewritten (a temporary file in the same folder, then a rename) with the events of the last
90 days, 20,000 lines at most.
Step report (run-status.json): {"time": the start of the run, "steps": {"<workflow step id>": "ok" | "skipped" | "running" | "failed" |
"failed: <message>"}}, rewritten atomically at each change, so that a run that dies leaves the step it was at. The message is French,
200 characters at most, without a file name.
Neither ever raises: a journal or a report that cannot be written must not stop the sorting (the error is printed once, to the
service's journal).
"""
import datetime, json, os, sys

PATH = None   # the journal; None: nothing is written
MAX_BYTES, KEEP_DAYS, KEEP_LINES = 5_000_000, 90, 20_000
LIMITS = {"object": 300, "from": 300, "to": 300, "detail": 1000}
rotated = False   # once per run: 20,000 long lines may stay above 5 MB, and rewriting the file at every event would be wasteful
complained = set()   # writes that failed and were already reported


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


RUN = now()   # the start of this run: the "run" of its events, the "time" of its report


def complain(what, e):
    if what not in complained:
        complained.add(what)
        print(f"{what} not written: {type(e).__name__} {e}", file=sys.stderr, flush=True)


def log(action, obj, src=None, dst=None, **fields):
    """Appends one event: src and dst become "from" and "to"; empty fields are left out."""
    global rotated
    if not PATH:
        return
    try:
        ev = {"time": now(), "action": action, "object": obj or "(sans nom)", "from": src, "to": dst, **fields}
        ev["run"] = ev.get("run") or RUN
        ev = {k: v for k, v in ev.items() if v not in (None, "", [])}
        if isinstance(ev.get("detail"), str):
            ev["detail"] = " ".join(ev["detail"].split())   # an error's output on one line
        for k, n in LIMITS.items():
            if isinstance(ev.get(k), str):
                ev[k] = ev[k][:n]
        if "confidence" in ev:
            ev["confidence"] = round(min(1.0, max(0.0, float(ev["confidence"]))), 3)
        line = json.dumps(ev, ensure_ascii=False)
        for c in "\x85  ":   # line breaks for some readers (str.splitlines): escaped, the JSON value stays the same
            line = line.replace(c, f"\\u{ord(c):04x}")
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8", "replace"))
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > MAX_BYTES and not rotated:
            rotated = True
            rotate()
    except Exception as e:
        complain("activity journal", e)


def rotate():
    """Keeps the events of the last 90 days, the last 20,000 at most. An unreadable line is dropped."""
    since = datetime.datetime.now().astimezone() - datetime.timedelta(days=KEEP_DAYS)
    kept = []
    with open(PATH, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                if datetime.datetime.fromisoformat(json.loads(line)["time"]) >= since:
                    kept.append(line.rstrip("\n") + "\n")
            except (ValueError, KeyError, TypeError):
                continue
    tmp = os.path.join(os.path.dirname(PATH), f".{os.path.basename(PATH)}.tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            f.writelines(kept[-KEEP_LINES:])
        os.replace(tmp, PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _failing():
    return os.path.join(os.path.dirname(PATH), f".{os.path.basename(PATH)}.failing")   # the start of a series of failed runs


def failed(obj, detail):
    """« échec » of a whole run, once per series: the runs that fail after it add nothing until one goes through (recovered)."""
    if not PATH:
        return
    try:
        if os.path.exists(_failing()):
            return
        with open(_failing(), "w", encoding="utf-8") as f:
            f.write(now())
    except Exception as e:
        complain("failure marker", e)
    log("échec", obj, detail=detail)


def recovered(obj):
    """A run that went through after failed ones: « rétabli », once."""
    if not PATH:
        return
    try:
        with open(_failing(), encoding="utf-8") as f:
            since = f.read().strip()
        os.remove(_failing())
    except FileNotFoundError:
        return
    except Exception as e:
        complain("failure marker", e)
        return
    try:
        since = datetime.datetime.fromisoformat(since).strftime("le %d/%m à %H:%M")
    except ValueError:
        since = "un moment"
    log("rétabli", obj, detail=f"de nouveau sans erreur, après des passages en échec depuis {since}")


class Report:
    """The step report of one run. steps: the workflow's step ids in order (a failure outside any running step goes to the first one
    without a state); path None: nothing is written."""

    def __init__(self, path, steps, time=None):
        self.path, self.order, self.time, self.steps = path, list(steps), time or RUN, {}

    def set(self, step, state, message=None):
        value = f"{state}: {' '.join(str(message).split())[:200]}" if message else state
        if self.steps.get(step) != value:
            self.steps[step] = value
            self.write()

    def running(self, step):
        self.set(step, "running")

    def current(self):
        """The step running, or None."""
        return next((s for s, v in self.steps.items() if v == "running"), None)

    def fail(self, message, step=None):
        """The step given, else the one running, else the first one without a state, fails with this message (French, no file name)."""
        step = step or self.current() or next((s for s in self.order if s not in self.steps), None)
        if step:
            self.set(step, "failed", message)

    def finish(self):
        """The end of a run that went through: a step still marked running is done."""
        self.steps = {s: "ok" if v == "running" else v for s, v in self.steps.items()}
        self.write()

    def write(self):
        if not self.path:
            return
        tmp = os.path.join(os.path.dirname(self.path), f".{os.path.basename(self.path)}.tmp")
        try:
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump({"time": self.time, "steps": self.steps}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            complain("step report", e)
            try:
                os.remove(tmp)
            except OSError:
                pass
