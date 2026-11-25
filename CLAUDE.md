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
6. **Push directly to `main`, no pull request** (Benjamin, 26/11/2026), after every check: a green push to `main` deploys automatically.

## Readability Standards (Mandatory, same in every repository)

Full text: `docs/engineering-standards.md` of the `home-server` repository (French → English vocabulary: `docs/decisions.md` of the `dashboard` repository).

1. **Limits**: functions ≤ 60 lines (100 with a comment that says why); lines ≤ 120 characters (100 in the Dashboard's JavaScript); early returns instead of nested `if`; no dead or commented-out code; no history in comments (git and the changelog keep it).
2. **Named structures**: `@dataclass(slots=True)` or `{ key, label }` objects, never `d[0]` or a hand-built nested dict; more than 3 parameters → an options object or keyword-only signature; named constants with their unit for every threshold, delay and size.
3. **Types and docs**: annotated signatures (Python), JSDoc `@typedef` for data shapes (JavaScript); a docstring or JSDoc that says WHY on every module and shared function.
4. **Errors and output**: precise exceptions, never swallowed in silence, `raise … from error`; `logging` instead of `print` outside interactive tools.
5. **Contracts**: a key, path or protocol word read by another program (often French) is never renamed on one side only; produce it from English attributes and read it through English locals.
6. **A readability pass changes no behaviour**: characterization test first, output compared before and after, `grep -rn` every rename across the repository (tools, tests, units, installers), entry points keep their name and command line, no bug fixed in passing (it goes in the report), one commit per module on `refactor/<language>-readability-<module>`, never pushed to `main` without the owner's validation. A new test must fail when the rule it pins is broken.
