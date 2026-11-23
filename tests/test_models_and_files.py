#!/usr/bin/env python3
"""Cross-platform offline unit tests for `sorter.journal` and `sorter.models` helpers."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `sorter.config` reads `settings.json`, `convention.md`, and `rules.md` beside `sorter/`.
# Build a temporary isolated copy using the tracked `*.example.*` templates so this test runs
# offline on both macOS and Ubuntu CI without requiring private config files.
_SANDBOX = tempfile.TemporaryDirectory()
_SANDBOX_ROOT = Path(_SANDBOX.name)
shutil.copytree(ROOT / "sorter", _SANDBOX_ROOT / "sorter")
for example_name, target_name in (
    ("settings.example.json", "settings.json"),
    ("convention.example.md", "convention.md"),
    ("rules.example.md", "rules.md"),
):
    shutil.copy2(ROOT / example_name, _SANDBOX_ROOT / target_name)
os.environ["FILE_SORTER_DATA"] = str(_SANDBOX_ROOT / "data")
(_SANDBOX_ROOT / "data").mkdir(exist_ok=True)
sys.path.insert(0, str(_SANDBOX_ROOT))

from sorter import journal, models  # noqa: E402


class TestJournalAndModels(unittest.TestCase):
    def test_kept_why_messages(self) -> None:
        self.assertIn("pièce jointe", journal.kept_why(0.95, {"_copy": True}))
        low = journal.kept_why(0.42, {"destination": "50 Travail", "raison": "document incomplet."})
        self.assertIn("42 %", low)
        self.assertIn("50 Travail", low)
        self.assertIn("document incomplet", low)
        outside = journal.kept_why(0.9, {"destination": "99 Inconnu"})
        self.assertIn("hors de l'arborescence", outside)

    def test_filed_emits_rename_event_only_when_name_changes(self) -> None:
        same = journal.filed("doc.pdf", "Drive · 00 À trier", "iCloud · 50 Travail/doc.pdf", {"raison": "ok"}, {}, "")
        self.assertEqual([ev[0] for ev in same], ["rangé"])

        renamed = journal.filed(
            "scan.pdf",
            "Drive · 00 À trier",
            "iCloud · 50 Travail/2026-11-23 EDF - Facture.pdf",
            {"raison": "facture"},
            {"engine": "local"},
            "par le Mac",
        )
        self.assertEqual([ev[0] for ev in renamed], ["rangé", "renommé"])
        self.assertEqual(renamed[1][2]["dst"], "2026-11-23 EDF - Facture.pdf")

    def test_mac_event_statuses(self) -> None:
        action = {
            "type": "ranger",
            "source": {"racine": "drive", "chemin": "00 À trier/a.pdf"},
            "cible": {"racine": "icloud", "chemin": "50 Travail/a.pdf"},
        }
        ev_ok = journal.mac_event(action, "ok")
        self.assertEqual(ev_ok[0], "rangé par le Mac")
        ev_missing = journal.mac_event(action, "absent")
        self.assertEqual(ev_missing[0], "annulé")
        ev_err = journal.mac_event(action, "erreur : permission denied")
        self.assertEqual(ev_err[0], "échec")
        self.assertIn("permission denied", ev_err[2]["detail"])

    def test_usage_limit_detection(self) -> None:
        self.assertEqual(
            models.usage_limit("usage_limit_reached: resets 3pm"),
            "limite d'usage de Claude atteinte, reprise vers 15:00",
        )
        self.assertEqual(
            models.usage_limit("Antigravity error: RESOURCE_EXHAUSTED"),
            "limite d'usage d'Antigravity atteinte",
        )
        self.assertIsNone(models.usage_limit("normal error"))


if __name__ == "__main__":
    unittest.main()
