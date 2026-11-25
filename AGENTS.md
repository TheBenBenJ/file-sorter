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
| `tests/test_sorter.py`, `tests/test_models_and_files.py` | Offline `python3 -I` test suites (run in `.github/workflows/ci.yml`) |
| `pyproject.toml` | Ruff linter and formatter configuration (PEP 8, Python 3.14 target, 120-char line length) |

## 2. Invariant Rules & Engineering Standards
- **Root entrypoint**: never rename or move `sort.py` (invoked directly by systemd/launchd jobs and the Mac relay). Keep `sort.py` re-exports intact.
- **Zero-dependency standard library**: keep all runtime code stdlib-only (`python3 -I` compatible).
- **Clean structure & typed models**: keep functions concise (~30–100 lines); use stdlib `@dataclass` models with `.to_dict()` for structured payloads rather than building ad-hoc nested dicts inline.
- **Contracts**: keep French log prefixes (`RANGÉ`, `GARDÉ`, `DOUBLON`, `DÉPOSÉ`, `VERSÉ`, `LAISSÉ`, `PLAN`, `MAC`, `ERREUR`, `SANS RÉPONSE`, `ATTENTE`, `TROP GROS`, `À DÉPOSER`, `BILAN`, `RELU`, `REVU`), French JSON contract keys (`plan.json`, `results.json`, `SCHEMA`, `REVIEW_SCHEMA`, `activity.jsonl`), and French user-facing prompts/notifications unchanged.
- **Language**: 100% of code, filenames, comments, docstrings, tests, docs, and commit messages in **English**.
- **Tests & docs**: every new rule, safeguard, or bug fix requires offline tests in `tests/test_*.py`, and `README.md`/`CLAUDE.md`/`AGENTS.md` must stay up to date.
- **Privacy & secrets**: never commit real `settings.json`, `convention.md`, `rules.md`, `review-rules.md`, `data/`, logs, or private filenames.
- **Push directly to `main`, no pull request** (Benjamin, 26/11/2026), after every check: a green push to `main` deploys automatically.
- **Validation**: before every commit, run:
  ```sh
  ruff check . && ruff format --check .
  python3 -m py_compile sort.py sorter/*.py tests/test_*.py
  python3 -I tests/test_models_and_files.py
  python3 -I tests/test_sorter.py
  ```
  (Note: `tests/test_sorter.py` expects Linux `notifier` for 2 notification assertions; CI runs all `tests/test_*.py` on Ubuntu.)
