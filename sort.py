#!/usr/bin/env python3
"""CLI entrypoint for file-sorter (inboxes sorting, Drive filing, and folder review).

Implementation lives in the `sorter/` package:
  - sorter/config.py   paths, settings, thresholds, text assets, and core helpers
  - sorter/files.py    inbox discovery, extraction, deduplication, safe paths, plan actions
  - sorter/drive.py    Google Drive write operations via rclone
  - sorter/models.py   LLM prompts, strict JSON schemas, local/Claude routing, shadow compare
  - sorter/journal.py  Dashboard activity events and French error formatting
  - sorter/review.py   theme folder review state machine and bilan reporting
  - sorter/runner.py   CLI modes (full Mac run, --decide, --apply, --deposit)
"""

import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from sorter.config import (  # noqa: E402, F401
    ARCHIVES,
    ARCHIVE_NAME,
    CLAUDE,
    CLEANUP,
    CONVENTION,
    DATA,
    DRIVE,
    DRIVE_INBOX,
    DRIVE_RCLONE,
    DRY,
    ICLOUD,
    INBOX,
    LOCAL_MODEL,
    LOCAL_URL,
    MAC,
    MODEL,
    PLAN,
    RESULTS,
    REVIEW,
    REVIEW_BILAN,
    REVIEW_BILAN_LINE,
    REVIEW_OWNER,
    ROLE,
    RULES,
    SETTINGS,
    SHADOW,
    THRESHOLD,
    archive_of,
    folders,
    is_archive,
    log,
    nfc,
    notify,
    read_json,
    write_json,
)
from sorter.drive import DriveError, drive_move, drive_unique, rclone  # noqa: E402, F401
from sorter.files import (  # noqa: E402, F401
    clean_name,
    dedupe,
    extract,
    jpeg_preview,
    load_kept,
    make_action,
    materialize,
    pending,
    place,
    relative,
    safe_dest,
    save_kept,
    sha,
    sig,
    to_trash,
    twin,
    unique,
    where,
)
from sorter.journal import (  # noqa: E402, F401
    downloads_events,
    filed,
    kept_why,
    mac_event,
    public_reason,
)
from sorter.models import (  # noqa: E402, F401
    FILING_ITEM,
    Question,
    SCHEMA,
    Unread,
    ask,
    ask_claude,
    ask_local,
    choose,
    claude_error,
    compare,
    described,
    filing,
    local_call,
    local_schema,
    match,
    named,
    outcome,
    prompt,
    simple,
    text_block,
    unread,
    usage_limit,
)
from sorter.review import (  # noqa: E402, F401
    CODE_MARKERS,
    COPY_WORDS,
    PHOTOS,
    REVIEW_ITEM,
    REVIEW_SCHEMA,
    Review,
    exact_copies,
    judge,
    listing,
    review_prompt,
    theme_walk,
)
from sorter.runner import (  # noqa: E402, F401
    apply_action,
    apply_plan,
    audit,
    batch,
    decide,
    deposit,
    main,
    move_downloads,
    note_download,
    prune,
    run_cli,
    stopped,
)

if __name__ == "__main__":
    run_cli()
