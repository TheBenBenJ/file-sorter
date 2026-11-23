# file-sorter — Agent Instructions

Operational guide for modifying `file-sorter` cleanly and safely. Full behavior reference: `README.md`.

## 1. Repository Map

| Path | Role |
|---|---|
| `sort.py` | Root CLI entrypoint (`--dry-run`, `--decide`, `--apply`, `--deposit`) and public re-exports (invoked by systemd/launchd jobs) |
| `sorter/activity.py` | Atomic JSONL activity journal (`activity.jsonl`) and `run-status.json` step report writer (read by Dashboard) |
| `sorter/config.py` | Environment variables, paths, thresholds, text assets (`settings.json`, `convention.md`, `rules.md`, `review-rules.md`), core JSON/log/notify helpers |
| `sorter/files.py` | Inbox discovery, OOXML/PDF/text extraction, deduplication, safe path checks, `plan.json` action builder |
| `sorter/drive.py` | Google Drive write operations via `rclone` (`DriveError`, `drive_unique`, `drive_move`) |
| `sorter/models.py` | LLM prompts, strict JSON schemas, local OpenAI-compatible model + Antigravity/Claude CLI routing, shadow comparison, validation |
| `sorter/journal.py` | Dashboard activity journal formatters (`activity.jsonl`) and public French error summaries (`run-status.json`) |
| `sorter/review.py` | Theme-folder review state machine (`Review`), confidence/copy safeguards, and `BILAN` logging |
| `sorter/runner.py` | Execution modes (`main`, `decide`, `apply_plan`, `apply_action`, `deposit`, `move_downloads`, `prune`) |
| `sorter/audit.py` | Read-only tidiness audit (`python3 -m sorter.audit`, writes `tidiness-state.json` for Dashboard) |
| `sorter/rename.py` | Bulk renaming helper (`python3 -m sorter.rename plan\|apply\|undo`) |
| `sorter/apply_plan.py` | Standalone tidying plan applier (`python3 -m sorter.apply_plan verify\|apply\|undo`) |
| `tests/test_sorter.py` | Offline sandbox test suite (`python3 -I tests/test_sorter.py`), also run in `.github/workflows/ci.yml` |
| `pyproject.toml` | Ruff linter and formatter configuration (PEP 8, Python 3.14 target, 120-char line length) |

## 2. Invariant Rules
- **Root entrypoint**: never rename or move `sort.py` (invoked directly by systemd/launchd jobs and the Mac relay). Keep `sort.py` re-exports intact.
- **Zero-dependency standard library**: keep all runtime code stdlib-only (no third-party pip dependencies).
- **Contracts**: keep French log prefixes (`RANGÉ`, `GARDÉ`, `DOUBLON`, `DÉPOSÉ`, `VERSÉ`, `LAISSÉ`, `PLAN`, `MAC`, `ERREUR`, `SANS RÉPONSE`, `ATTENTE`, `TROP GROS`, `À DÉPOSER`, `BILAN`, `RELU`, `REVU`), French JSON contract keys (`plan.json`, `results.json`, `SCHEMA`, `REVIEW_SCHEMA`, `activity.jsonl`), and French user-facing prompts/notifications unchanged.
- **Language**: all code, filenames, comments, docstrings, and commit messages in **English**.
- **Privacy & secrets**: never commit real `settings.json`, `convention.md`, `rules.md`, `review-rules.md`, `data/`, logs, or private filenames.
- **Validation**: before every commit, run:
  ```sh
  ruff check . && ruff format --check .
  python3 -m py_compile sort.py sorter/*.py tests/test_sorter.py
  python3 -I tests/test_sorter.py
  ```
  (Note: `tests/test_sorter.py` expects Linux `notifier` for 2 notification assertions; CI runs `python -I tests/test_sorter.py` on Ubuntu.)
