# file-sorter

*Formerly `tri-icloud` (renamed on 03/11/2026, with its folders, log and environment variables; its scripts, files, options and subcommands got English names the same day).*

Automatic filing of personal documents into a numbered folder tree on iCloud Drive (and Google Drive for office files). Claude proposes a destination and a clean name for each new file; a plain Python script validates the proposal and moves the file. Nothing is ever overwritten, and when in doubt the file stays where it is.

Why: documents arrive from many places (the phone, the browser's Downloads, invoices received by mail) and end up as `Document (1).pdf` in a pile. Filing them by hand is a chore; letting an AI move files on its own is a risk. Here the AI only reads and proposes, and the script decides.

The code, its file names and its options are in English. The documents' folders and names, the naming convention and the prompt are French, because the tree and the names it produces are French (`00 À trier`, `50 Travail`, `2026-01-15 Fournisseur - Facture 42.pdf`).

## How it works

Inboxes: Google Drive `00 À trier`, the main one (the Mac pours the documents of its `~/Downloads` into it); Google Drive `00 Factures entrantes`, filled every hour by an Apps Script that imports invoices from Gmail (`gmail-invoices.gs`, kept out of this repo); iCloud `00 À trier`, where the phone's downloads land (on an iPhone, neither Safari nor Shortcuts can choose a Google Drive folder); and, read last, Google Drive `00 Pièces jointes`, where the owner's mail sorter (a separate, private repo) exports every document attached to his mails, received and sent, once per content, named `<date of the mail> <subject> - <original name>` so that the date and the subject help the decision.

For each file at least 60 seconds old (15 per pass): a short text extract (`pdftotext`, `textutil`, the XML inside office files) or an image preview, then one `claude -p` call with the Read tool only. Claude answers in JSON for each file: destination, new name, confidence, reason. The script validates and acts:

- confidence below 0.7, or a destination outside the existing tree (one new sub-folder level is allowed): the file stays in its `00 À trier` (an invoice from Gmail, or a download in the original mode, is moved to the Drive's; a mail attachment stays in `00 Pièces jointes`) and is remembered in `kept.json` so it is not asked about again;
- never overwrite: a name already taken gets ` (2)`;
- an exact copy (SHA-1) of a document already filed in iCloud goes to the Trash, never deleted;
- editable files (`.docx`, `.xlsx`, `.pptx`…) go to the same path in the Drive, where the Apps Script converts them to Google Docs and Sheets; finished documents (PDF, images) go to iCloud, the archive;
- photos and screenshots keep their name;
- from `~/Downloads`, only documents (PDF, images, office and iWork files, text, CSV) of 50 MB at most go to the Drive; installers, archives, videos and bigger files stay in Downloads (a too big document is reported once), are never deleted, and are listed on the dashboard after 7 days.

### Two ways to run it

**Split (in service): a home server decides and files the Drive, the Mac files iCloud.**

```
   Server (Linux, timer every 5 min)                                      Mac (relay agent every 5 min)
 Drive 00 À trier ──────┐ read-only mounts
 Drive 00 Factures ─────┤
   entrantes            ├─► sort.py --decide ──┬─► inside the Drive, itself (rclone):
 iCloud 00 À trier ─────┤    (Claude, Read)    │   editable files filed, doubtful invoices
 Drive 00 Pièces ───────┘                      │   into 00 À trier, duplicates to its trash
   jointes (mails)                             │
                                               └─► plan.json ───────── pulled ──────► sort.py --apply
                                                                                      ├─ checks every action again
                        ◄────────────── results.json ◄──────── pushed ────────────────┤─ moves into iCloud (from the Drive's
                                                                                      │  inboxes through Google Drive for desktop)
                                                                                      ├─ duplicates to the Trash
                                                                                      └─ documents of ~/Downloads ──► Drive 00 À trier
 vat check ──► sort.py --deposit icloud <path> <file> ──► "deposer" action ──► the Mac writes the report
```

The server reads iCloud and the Drive through read-only mounts. Inside the Drive it files itself, through an rclone remote allowed to write it (`FILE_SORTER_DRIVE_RCLONE`): so the Drive is sorted even when the Mac sleeps. Into or out of iCloud, only the Mac moves: it fetches the plan, applies it and sends back what it did; a finished document waiting in a Drive inbox reaches iCloud that way, the Mac reading it through Google Drive for desktop. The server never connects to the Mac: when the Mac sleeps, those files simply wait in their inbox.

Plan actions (`plan.json`): `ranger` (file it at `cible`), `doublon` (to the Trash), `vers_a_trier` (a doubtful invoice into the Drive's `00 À trier`, when the server cannot move it itself), `deposer` (write a document handed over by the server). An action's id is a hash of type, root, path and size. The Mac answers in `results.json` with `ok`, `absent`, `modifié depuis la décision` or `erreur : …`, and says nothing yet for an iCloud file that is not downloaded.

**Original: everything on the Mac.** `sort.py` alone decides and moves (a launchd agent watching the inboxes, and every 30 minutes). `sort.py --dry-run` shows what it would do. This mode is also the rollback of the split one; the two never run at the same time.

### Other scripts

- `audit.py`: no AI; writes `tidiness-state.json` (loose files next to sub-folders, names outside the convention, files waiting in an inbox for more than 7 days, `00 Pièces jointes` excepted: it keeps doubtful attachments on purpose) for a dashboard. `sort.py` runs it after each pass on the Mac.
- `rename.py plan|apply|undo`: bulk renaming of existing documents according to `convention.md`. `plan` writes `data/rename-plan.json` and a `.tsv` to review, `apply` keeps an undo journal (`data/rename-undo.json`), `undo` restores the names.
- `apply_plan.py verify|apply|undo`: applies a tidying plan (a JSON list of `move` and `trash` operations) after checks, with an undo journal in `data/` (`undo-tidy-<date>.json`).

## Setup

Requirements: Python 3.9+ (standard library only), the Claude Code CLI (`claude`), `pdftotext` (poppler). On macOS, `trash`, `brctl`, `sips` and `textutil` come with the system; on Linux, `heif-convert` (libheif) reads iPhone photos.

```sh
cp settings.example.json settings.json   # owner description, Drive path, folders where loose files are fine
cp rules.example.md rules.md             # filing rules, injected into the prompt
cp convention.example.md convention.md   # naming convention, injected into the prompt
mkdir data                               # journals of rename.py and apply_plan.py
python3 sort.py --dry-run
```

`settings.json` keys: `proprietaire` (who the documents belong to, quoted in the prompt), `drive` (Drive path relative to the home folder), `paquets` (folders the audit skips) and `vrac_autorise` (folders where loose files are fine).

| Variable | Meaning | Default (the Mac) |
|---|---|---|
| `FILE_SORTER_ICLOUD` | iCloud Drive root | `~/Library/Mobile Documents/com~apple~CloudDocs` |
| `FILE_SORTER_DRIVE` | Google Drive root | `~/` + `drive` of `settings.json` |
| `FILE_SORTER_DATA` | folder of `plan.json`, `results.json`, `applied.json`, `kept.json`, `sort.lock` | the script's folder |
| `FILE_SORTER_INBOX` | another iCloud inbox, for a trial | iCloud `00 À trier` |
| `FILE_SORTER_DRIVE_INBOX` | another Drive inbox, for a trial | Drive `00 À trier` |
| `FILE_SORTER_ATTACHMENTS_INBOX` | another inbox of mail attachments, for a trial | Drive `00 Pièces jointes` |
| `FILE_SORTER_DRIVE_RCLONE` | server: rclone remote allowed to write the Drive (`gsort:`); unset, only the Mac moves | unset |
| `FILE_SORTER_DOWNLOADS_MAX_MB` | largest download poured into the Drive | `50` |
| `FILE_SORTER_CLAUDE` | `claude` binary | `/opt/homebrew/bin/claude` |
| `FILE_SORTER_MODEL` | model | `haiku` |

`rename.py` and `apply_plan.py` also read `ROOT` (another root, e.g. the Drive), and `rename.py` reads `SUFFIX` (separate plan files), `EXCLUDED` (folders to skip, separated by `|`) and `DOCS_ONLY` (rename documents only).

Scheduling: on the server, a timer runs `python3 -I sort.py --decide` with `FILE_SORTER_ICLOUD`, `FILE_SORTER_DRIVE` and `FILE_SORTER_DATA` pointing at the read-only mounts and a data folder; the personal files are copied next to the code. On the Mac, an agent fetches `plan.json` into this folder, runs `sort.py --apply` and sends `results.json` back. (The units and the relay live in a separate, private repo.)

The file names above are shared between the server, the Mac and a dashboard, and the JSON keys inside the files are French: both are a contract. Rename a file only together with every program that reads it, and keep the keys.

## Operations and troubleshooting

Logs: on the Mac `~/Library/Logs/file-sorter.log`; on the server the service journal. Each line starts with a French keyword that docs and habits search for: `RANGÉ` (filed), `GARDÉ` (left in `00 À trier`), `DOUBLON` (duplicate to the Trash), `DÉPOSÉ` (document written), `VERSÉ` (a download poured into the Drive's `00 À trier`), `LAISSÉ` (a download that is not a document, left in Downloads), `PLAN` (server decision), `MAC` (status returned by the Mac), `ERREUR`, `SANS RÉPONSE` (no answer for that file, retried), `ATTENTE` (file or folder not available yet, retried), `TROP GROS`, `À DÉPOSER`. Notifications are French too: macOS notifications on the Mac, a `notifier` command on the server.

| Symptom | Cause | Remedy |
|---|---|---|
| « Resource deadlock avoided » (Errno 11) | the iCloud or Drive file is only in the cloud, and a launchd job may not download it by default | `sort.py` allows itself to (`setiopolicy_np`, at start on the Mac); `materialize()` also asks `brctl download` for iCloud and retries on the next pass |
| A file is never filed | it was left (`GARDÉ`) or Claude skipped it (`SANS RÉPONSE`) | read the log; remove its line from `kept.json` (server: `name|size`, Mac: `name|size|mtime`) to have it examined again |
| A file waits in `00 À trier` | Mac asleep, plan not fetched yet, mounts a few minutes behind | wait for the next pass, or run the relay by hand; an action not returned within 7 days is dropped by the server |
| A rule or convention change has no effect | the server has its own copy of the personal files | copy `settings.json`, `rules.md` and `convention.md` to the server again |

Before deploying a change: `python3 -I -m py_compile sort.py`, then `python3 -I sort.py --dry-run` on the Mac, or `--decide` with `FILE_SORTER_INBOX` and `FILE_SORTER_DATA` pointing at trial folders.

`gmail-invoices.gs` is deployed by hand in Google Apps Script and is kept out of this repo (personal mail queries and folder ids). Automation never modifies it; a change is pasted into the Apps Script editor by a person.

## Security

- No secrets in the repo. Personal files (`settings.json`, `rules.md`, `convention.md`, `kept.json`, `gmail-invoices.gs`) and all state files are ignored by git, under their former French names too; the `*.example.*` files show their format.
- Documents are untrusted input, and a document can try to give instructions. Claude runs with `--tools Read --allowedTools Read` (it can read, not write, move or run anything), `--strict-mcp-config` with no MCP server (no connectors: no mail, no drive, no web), `--setting-sources project` (no user settings, hooks or permissions), `--no-session-persistence`, and `--add-dir` limited to the inboxes and the preview folder. Its answer is data that the script checks; the worst a crafted document can do is get itself filed in a wrong existing folder or left in the inbox.
- The server reads through read-only mounts and cannot write into iCloud. In the Drive it writes only through `sort.py`'s own calls: a file of an inbox moved into the tree (the destination validated as for the Mac: an existing folder or one new level, a cleaned name, never an inbox; the name checked against Google first, so nothing is overwritten), a doubtful invoice moved into `00 À trier`, or a duplicate sent to the Drive's trash; nothing is deleted for good. Google has no narrower grant than the whole Drive for files the server did not create, so the grant is kept in the server account that runs the sorting and nowhere else.
- The Mac checks every action again: paths must stay inside iCloud or the Drive, the source must be in an inbox, the target never an inbox, and the size must not have changed since the decision.
- Server notifications go to a public push service, so they never contain file names.
- Text extracts (up to 2,500 characters) and unreadable files are sent to Claude: judge whether that suits your documents.

## Design decisions

- **The model proposes, the script decides.** Rather than letting an agent move files with write tools, Claude only reads and answers in a fixed JSON schema; the script accepts only existing folders (plus one new level), a confidence of at least 0.7, cleaned names and never an overwrite. Doubt means the file stays.
- **The server decides, the Mac moves** (adopted on 03/11/2026, the owner's idea), **and the server files the Drive itself** (the owner's decision the same afternoon). The main inbox is in the Drive, where the Mac pours its downloads (the phone's land in iCloud's inbox, the only kind of folder iOS lets Safari and Shortcuts choose): editable documents stay in the Drive and are filed there by the server, even with the Mac asleep; finished documents go to iCloud, the archive, through the Mac. Rejected: the server writing into iCloud with rclone, whose iCloud backend is still experimental and whose repeated failures could lock the Apple account. Cost: a grant on the whole Drive on the server (see Security), and finished documents still wait for the Mac.
- **Downloads: documents only, 50 MB at most.** Everything poured into the Drive is uploaded, and a finished document comes back down when the Mac files it in iCloud: installers, archives and videos have no place in the archive, and a big file would cost two transfers for nothing. They stay in Downloads, never deleted by the sorting, and the dashboard lists them after a week.
- **Idempotent plan.** Action ids come from the content (type, path, size); the Mac remembers applied ids for 7 days (`applied.json`), so a plan fetched twice is applied once; the server drops actions never returned within a week and remembers handled sources for an hour, because the mounts show changes a few minutes late.
- **`kept.json` signatures differ by machine**: `name|size|mtime` on the Mac, `name|size` on the server, because a file the Mac moves keeps its name and size but not its date.
- **Mail attachments: a fourth inbox, read last, that keeps its doubtful files** (03/11/2026, the owner's wish that every attachment of his mail goes through the sorting, the backlog once, then new mail). The mail sorter exports each document once into the Drive's `00 Pièces jointes`, at about the pace of the sorting (40 files every 15 minutes, 15 decided every 5). A doubtful attachment stays there, remembered in `kept.json`, instead of joining `00 À trier`: a mailbox's backlog (some 1,600 files here) would bury the documents the owner drops himself; for the same reason the folder is read after the other inboxes and the audit does not list its files as waiting. Duplicates of the archive go to the Drive's trash, like any other inbox's. An attachment the model proposes no new name for stays too: its export name never follows the convention, and the model does that when it recognizes a copy of another document of the batch (the same photo sent again, re-encoded), which its answer cannot send to the trash; only identical bytes go there. Files already planned for the Mac no longer count among a pass's 15: they held back the next ones while the Mac slept or the mounts lagged.
- **Downloads go through the Drive's `00 À trier`.** The server cannot see the Mac's Downloads; pouring them into the single inbox keeps one place where decisions are made.
- **Duplicates by size, then SHA-1**: only files of the same size are hashed, which keeps the walk of iCloud cheap.
- **One lock per data folder**: a pass that finds another running exits; `--deposit` waits for the lock, so a report is never lost.
- **Haiku, 15 files per call**: cheap and fast enough for a pass every 5 minutes; a pass loops until the inboxes are empty.
- **French where it is a contract**: the prompt and its answer keys (the names must stay French), the keys and values inside the plan and result files exchanged between two machines updated separately, the log keywords and the notifications. The code was translated to English on 03/11/2026 without changing any of them.
- **English names for files, options and subcommands** (03/11/2026, the owner's wish: everything technical in English). A file name has a few known readers (the Mac relay, the server's units, the dashboard, two skills), so the names changed in one switch together with those readers and the files on both machines. The JSON keys stayed French: they live inside the files the two machines exchange, so changing them would need both sides to read both forms for a while. An unknown option now stops `sort.py` (it used to fall back to the full mode, which decides and moves), and an unknown command stops `apply_plan.py` (it used to only check): a caller left with a former name fails loudly.

## License

MIT, see [LICENSE](LICENSE).
