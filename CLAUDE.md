# file-sorter

Automatic filing and review of personal documents across iCloud Drive and Google Drive (`sort.py` + `sorter/`). Full architecture & operations: [`README.md`](README.md). Repository map & contracts: [`AGENTS.md`](AGENTS.md).

## Engineering Standards (Mandatory)

1. **Typed domain models — no ad-hoc complex JSON dicts**:
   - Prefer stdlib `@dataclass` models with explicit `.to_dict()` serialization for structured plans, review decisions, and journal events instead of assembling nested untyped dicts inline.
2. **Modular, readable code (stdlib only, `python3 -I`)**:
   - Keep functions focused and concise (~30–100 lines); place domain logic in `sorter/` (`config`, `files`, `drive`, `models`, `journal`, `review`, `runner`, `activity`, `audit`, `rename`, `apply_plan`) and keep `sort.py` as the stable CLI entrypoint/re-exporter.
   - Zero third-party Python dependencies; all code must run cleanly under `python3 -I`.
3. **100% English technical code**:
   - All code identifiers, filenames, classes, functions, comments, docstrings, tests, documentation, and commit messages in **English**.
   - Keep French folder names (`00 À trier`, `90 Archives`), French log keywords (`RANGÉ`, `GARDÉ`, `DOUBLON`, etc.), French JSON contract keys (`plan.json`, `results.json`, `SCHEMA`, `REVIEW_SCHEMA`, `activity.jsonl`), and French user-facing prompts/notifications unchanged.
4. **Mandatory offline tests & up-to-date docs**:
   - Every new feature, safeguard, or bug fix MUST include offline `python3 -I` tests in `tests/test_*.py` (no network, no real cloud drives or accounts).
   - Update `README.md`, `CLAUDE.md`, and `AGENTS.md` whenever architecture, AI providers, entrypoints, or tests change.
5. **Required checks before every commit**:
   ```sh
   ruff check . && ruff format --check .
   python3 -m py_compile sort.py sorter/*.py tests/test_*.py
   python3 -I tests/test_models_and_files.py
   python3 -I tests/test_sorter.py   # full sandbox suite (runs in Linux CI)
   ```
6. **Never push directly to `main`** without Benjamin's explicit confirmation (a green push to `main` auto-promotes `production` and deploys to `pc-benjamin`).
