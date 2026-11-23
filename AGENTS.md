# file-sorter — Agent Instructions

Operational guide for modifying `file-sorter` cleanly and safely. Full behavior reference: `README.md`.

## 1. Repository Map

| Path | Role |
|---|---|
| `sort.py` | Root CLI entrypoint (`--dry-run`, `--decide`, `--apply`, `--deposit`) and public re-exports (used by `rename.py` and systemd/launchd jobs) |
| `sorter/config.py` | Environment variables, paths, thresholds, text assets (`settings.json`, `convention.md`, `rules.md`, `review-rules.md`), core JSON/log/notify helpers |
| `sorter/files.py` | Inbox discovery, OOXML/PDF/text extraction, deduplication, safe path checks, `plan.json` action builder |
| `sorter/drive.py` | Google Drive write operations via `rclone` (`DriveError`, `drive_unique`, `drive_move`) |
| `sorter/models.py` | LLM prompts, strict JSON schemas, local OpenAI-compatible model + Claude CLI routing, shadow comparison, validation |
| `sorter/journal.py` | Dashboard activity journal formatters (`activity.jsonl`) and public French error summaries (`run-status.json`) |
| `sorter/review.py` | Theme-folder review state machine (`Review`), confidence/copy safeguards, and `BILAN` logging |
| `sorter/runner.py` | Execution modes (`main`, `decide`, `apply_plan`, `apply_action`, `deposit`, `move_downloads`, `prune`) |
| `activity.py` | Atomic JSONL activity journal and `run-status.json` step report writer (read by Dashboard) |
| `audit.py` | Read-only tidiness audit (`tidiness-state.json` for Dashboard) |
| `rename.py` | Bulk renaming helper (`plan`, `apply`, `undo`) importing helpers from `sort` |
| `apply_plan.py` | Standalone tidying plan applier (`verify`, `apply`, `undo`) |
| `tests.py` | Offline sandbox test suite (`python3 -I tests.py`), also run in `.github/workflows/ci.yml` |

## 2. Invariant Rules
- **Root entrypoints & re-exports**: never rename or move `sort.py`, `activity.py`, `audit.py`, `rename.py`, `apply_plan.py`, or `tests.py`. Keep `sort.py` re-exports intact for `rename.py`.
- **Zero-dependency standard library**: keep all runtime code stdlib-only (no third-party pip dependencies).
- **Contracts**: keep French log prefixes (`RANGÉ`, `GARDÉ`, `DOUBLON`, `DÉPOSÉ`, `VERSÉ`, `LAISSÉ`, `PLAN`, `MAC`, `ERREUR`, `SANS RÉPONSE`, `ATTENTE`, `TROP GROS`, `À DÉPOSER`, `BILAN`, `RELU`, `REVU`), French JSON contract keys (`plan.json`, `results.json`, `SCHEMA`, `REVIEW_SCHEMA`, `activity.jsonl`), and French user-facing prompts/notifications unchanged.
- **Language**: all code, filenames, comments, docstrings, and commit messages in **English**.
- **Privacy & secrets**: never commit real `settings.json`, `convention.md`, `rules.md`, `review-rules.md`, `data/`, logs, or private filenames.
- **Validation**: before every commit, run:
  ```sh
  python3 -m py_compile sort.py activity.py audit.py apply_plan.py rename.py tests.py sorter/*.py
  python3 -I tests.py
  ```
  (Note: `tests.py` expects Linux `notifier` for 2 notification assertions; CI runs `python -I tests.py` on Ubuntu.)
