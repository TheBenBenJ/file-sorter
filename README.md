# file-sorter

*Formerly `tri-icloud` (renamed on 03/11/2026). The installed folder keeps that name, `~/.local/share/tri-icloud`, because it also holds the tool's state, which other programs read.*

Automatic filing of personal documents into a numbered folder tree on iCloud Drive (and Google Drive for office files). Claude proposes a destination and a clean name for each new file; a plain Python script validates the proposal and moves the file. Nothing is ever overwritten, and when in doubt the file stays where it is.

Why: documents arrive from many places (the phone, the browser's Downloads, invoices received by mail) and end up as `Document (1).pdf` in a pile. Filing them by hand is a chore; letting an AI move files on its own is a risk. Here the AI only reads and proposes, and the script decides.

The code is in English. Folder names, file names, the naming convention and the prompt are French, because the tree and the names it produces are French (`00 À trier`, `50 Travail`, `2026-01-15 Fournisseur - Facture 42.pdf`).

## How it works

Inboxes: iCloud `00 À trier`, `~/Downloads` on the Mac, and Google Drive `00 Factures entrantes`, filled every hour by an Apps Script that imports invoices from Gmail (`gmail-factures.gs`, kept out of this repo).

For each file at least 60 seconds old (15 per pass): a short text extract (`pdftotext`, `textutil`, the XML inside office files) or an image preview, then one `claude -p` call with the Read tool only. Claude answers in JSON for each file: destination, new name, confidence, reason. The script validates and acts:

- confidence below 0.7, or a destination outside the existing tree (one new sub-folder level is allowed): the file stays in `00 À trier` (files from Downloads or the Drive inbox are moved there) and is remembered in `gardes.json` so it is not asked about again;
- never overwrite: a name already taken gets ` (2)`;
- an exact copy (SHA-1) of a document already filed in iCloud goes to the Trash, never deleted;
- office files (`.docx`, `.xlsx`…) go to the same path in the Drive, where the Apps Script converts them to Google Docs and Sheets; finished documents (PDF, images) go to iCloud, the archive;
- photos and screenshots keep their name; files over 500 MB stay in Downloads, reported once.

### Two ways to run it

**Split (in service): a home server decides, the Mac moves.**

```
   Server (Linux, timer every 5 min)                              Mac (relay agent every 5 min)
 iCloud 00 À trier ──┐ read-only
 Drive 00 Factures ──┼─► tri.py --decider ──► tri-plan.json ──── pulled ─────► tri.py --appliquer
   entrantes         │    (Claude, Read only)                                  ├─ checks every action again
                     │                                                         ├─ moves into iCloud / the Drive
                     └──◄ tri-resultats.json ◄──────────────── pushed ─────────┤  duplicates to the Trash
                                                                               └─ ~/Downloads ──► 00 À trier
 vat check ──► tri.py --deposer icloud <path> <file> ──► "deposer" action ──► the Mac writes the report
```

The server reads iCloud and the Drive through read-only mounts and writes a plan. The Mac, the only machine that writes into iCloud, fetches the plan, applies it and sends back what it did. The server never connects to the Mac: when the Mac sleeps, files simply wait in their inbox.

Plan actions (`tri-plan.json`): `ranger` (file it at `cible`), `doublon` (to the Trash), `vers_a_trier` (from the Drive inbox to iCloud `00 À trier`, when unsure), `deposer` (write a document handed over by the server). An action's id is a hash of type, root, path and size. The Mac answers in `tri-resultats.json` with `ok`, `absent`, `modifié depuis la décision` or `erreur : …`, and says nothing yet for an iCloud file that is not downloaded.

**Original: everything on the Mac.** `tri.py` alone decides and moves (a launchd agent watching the inboxes, and every 30 minutes). `tri.py --dry-run` shows what it would do. This mode is also the rollback of the split one; the two never run at the same time.

### Other scripts

- `audit-rangement.py`: no AI; writes `etat-rangement.json` (loose files next to sub-folders, names outside the convention, files waiting in an inbox for more than 7 days) for a dashboard. `tri.py` runs it after each pass on the Mac.
- `renommer.py plan|appliquer|annuler`: bulk renaming of existing documents according to `convention.md`. `plan` writes `donnees/plan-renommage.json` and a `.tsv` to review, `appliquer` keeps an undo journal, `annuler` restores the names.
- `appliquer-plan.py verifier|appliquer|annuler`: applies a tidying plan (a JSON list of `move` and `trash` operations) after checks, with an undo journal in `donnees/`.

## Setup

Requirements: Python 3.9+ (standard library only), the Claude Code CLI (`claude`), `pdftotext` (poppler). On macOS, `trash`, `brctl`, `sips` and `textutil` come with the system; on Linux, `heif-convert` (libheif) reads iPhone photos.

```sh
cp reglages.exemple.json reglages.json   # owner description, Drive path, folders where loose files are fine
cp regles.exemple.md regles.md           # filing rules, injected into the prompt
cp convention.exemple.md convention.md   # naming convention, injected into the prompt
mkdir donnees
python3 tri.py --dry-run
```

`reglages.json` keys: `proprietaire` (who the documents belong to, quoted in the prompt), `drive` (Drive path relative to the home folder), `paquets` (folders the audit skips) and `vrac_autorise` (folders where loose files are fine).

| Variable | Meaning | Default (the Mac) |
|---|---|---|
| `TRI_ICLOUD` | iCloud Drive root | `~/Library/Mobile Documents/com~apple~CloudDocs` |
| `TRI_DRIVE` | Google Drive root | `~/` + `drive` of `reglages.json` |
| `TRI_DONNEES` | folder of `tri-plan.json`, `tri-resultats.json`, `tri-appliques.json`, `gardes.json`, `tri.lock` | the script's folder |
| `TRI_A_TRIER` | another inbox, for a trial | iCloud `00 À trier` |
| `TRI_CLAUDE` | `claude` binary | `/opt/homebrew/bin/claude` |
| `TRI_MODEL` | model | `haiku` |

`renommer.py` and `appliquer-plan.py` also read `RACINE` (another root, e.g. the Drive), and `renommer.py` reads `SUFFIXE` (separate plan files), `EXCLUS` (folders to skip, separated by `|`) and `DOCS_SEULEMENT` (rename documents only).

Scheduling: on the server, a timer runs `python3 -I tri.py --decider` with `TRI_ICLOUD`, `TRI_DRIVE` and `TRI_DONNEES` pointing at the read-only mounts and a data folder; the personal files are copied next to the code. On the Mac, an agent fetches `tri-plan.json` into this folder, runs `tri.py --appliquer` and sends `tri-resultats.json` back. (The units and the relay live in a separate, private repo.)

The file names and JSON keys above are French and shared between the server, the Mac and a dashboard: they are a contract, do not rename them.

## Operations and troubleshooting

Logs: on the Mac `~/Library/Logs/tri-icloud.log`; on the server the service journal. Each line starts with a French keyword that docs and habits search for: `RANGÉ` (filed), `GARDÉ` (left in `00 À trier`), `DOUBLON` (duplicate to the Trash), `DÉPOSÉ` (document written), `VERSÉ` (Downloads poured into `00 À trier`), `PLAN` (server decision), `MAC` (status returned by the Mac), `ERREUR`, `SANS RÉPONSE` (no answer for that file, retried), `ATTENTE` (file or folder not available yet, retried), `TROP GROS`, `À DÉPOSER`. Notifications are French too: macOS notifications on the Mac, a `notifier` command on the server.

| Symptom | Cause | Remedy |
|---|---|---|
| « Resource deadlock avoided » (Errno 11) | the iCloud or Drive file is only in the cloud and a launchd job may not download it | `materialize()` asks `brctl download` and retries; keep the Drive inbox « available offline » |
| A file is never filed | it was left (`GARDÉ`) or Claude skipped it (`SANS RÉPONSE`) | read the log; remove its line from `gardes.json` (server: `name|size`, Mac: `name|size|mtime`) to have it examined again |
| A file waits in `00 À trier` | Mac asleep, plan not fetched yet, mounts a few minutes behind | wait for the next pass, or run the relay by hand; an action not returned within 7 days is dropped by the server |
| A rule or convention change has no effect | the server has its own copy of the personal files | copy `reglages.json`, `regles.md` and `convention.md` to the server again |

Before deploying a change: `python3 -I -m py_compile tri.py`, then `python3 -I tri.py --dry-run` on the Mac, or `--decider` with `TRI_A_TRIER` and `TRI_DONNEES` pointing at trial folders.

`gmail-factures.gs` is deployed by hand in Google Apps Script and is kept out of this repo (personal mail queries and folder ids). Automation never modifies it; a change is pasted into the Apps Script editor by a person.

## Security

- No secrets in the repo. Personal files (`reglages.json`, `regles.md`, `convention.md`, `gardes.json`, `gmail-factures.gs`) and all state files are ignored by git; the `*.exemple.*` files show their format.
- Documents are untrusted input, and a document can try to give instructions. Claude runs with `--tools Read --allowedTools Read` (it can read, not write, move or run anything), `--strict-mcp-config` with no MCP server (no connectors: no mail, no drive, no web), `--setting-sources project` (no user settings, hooks or permissions), `--no-session-persistence`, and `--add-dir` limited to the inboxes and the preview folder. Its answer is data that the script checks; the worst a crafted document can do is get itself filed in a wrong existing folder or left in the inbox.
- The server only has read-only mounts: it cannot write into iCloud or the Drive. Only the Mac writes, and it checks every action again: paths must stay inside iCloud or the Drive, the source must be in an inbox, the target never an inbox, and the size must not have changed since the decision.
- Server notifications go to a public push service, so they never contain file names.
- Text extracts (up to 2,500 characters) and unreadable files are sent to Claude: judge whether that suits your documents.

## Design decisions

- **The model proposes, the script decides.** Rather than letting an agent move files with write tools, Claude only reads and answers in a fixed JSON schema; the script accepts only existing folders (plus one new level), a confidence of at least 0.7, cleaned names and never an overwrite. Doubt means the file stays.
- **The server decides, the Mac moves** (adopted on 03/11/2026, the owner's idea). Rejected: the server writing into iCloud with rclone, whose iCloud backend is still experimental and whose repeated failures could lock the Apple account; and moving the inbox to the Drive so the server could move files itself, which still needed iCloud writes and a full Drive authorization. The server now needs neither, and only sends instructions. Cost: nothing moves while the Mac sleeps; nothing is lost either.
- **Idempotent plan.** Action ids come from the content (type, path, size); the Mac remembers applied ids for 7 days (`tri-appliques.json`), so a plan fetched twice is applied once; the server drops actions never returned within a week and remembers handled sources for an hour, because the mounts show changes a few minutes late.
- **`gardes.json` signatures differ by machine**: `name|size|mtime` on the Mac, `name|size` on the server, because a file the Mac moves keeps its name and size but not its date.
- **Downloads go through `00 À trier`.** The server cannot see the Mac's Downloads; pouring them into the iCloud inbox keeps one place where decisions are made.
- **Duplicates by size, then SHA-1**: only files of the same size are hashed, which keeps the walk of iCloud cheap.
- **One lock per data folder**: a pass that finds another running exits; `--deposer` waits for the lock, so a report is never lost.
- **Haiku, 15 files per call**: cheap and fast enough for a pass every 5 minutes; a pass loops until the inboxes are empty.
- **French where it is a contract**: the prompt and its answer keys (the names must stay French), the plan and result files exchanged between two machines updated separately, the log keywords and the notifications. The code was translated to English on 03/11/2026 without changing any of them.

## License

MIT, see [LICENSE](LICENSE).
