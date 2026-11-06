import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.provider_snapshot_api import (
    claude_snapshot,
    codex_snapshot,
    count_text,
    epoch,
    percent,
    safe_failure,
    safe_window,
)


class SnapshotTests(unittest.TestCase):
    def test_codex_snapshot_converts_used_percent_to_remaining(self):
        snapshot = codex_snapshot({
            "rateLimits": {
                "primary": {"usedPercent": 25, "resetsAt": 1_800_000_000},
                "secondary": {"usedPercent": 100.4},
            },
            "rateLimitResetCredits": {"availableCount": 2},
        })
        self.assertEqual(snapshot["primary"], {"remainingPercent": 75, "resetsAt": 1_800_000_000.0})
        self.assertEqual(snapshot["weekly"]["remainingPercent"], 0)
        self.assertEqual(snapshot["resetCount"], 2)

    def test_codex_snapshot_without_windows_is_an_error(self):
        with self.assertRaises(ValueError):
            codex_snapshot({"rateLimits": {}})

    def test_boolean_values_are_not_numbers(self):
        self.assertIsNone(safe_window({"usedPercent": True}))
        self.assertEqual(count_text(True), "—")
        self.assertEqual(percent(True), "—")

    def test_claude_snapshot_keeps_only_known_models_and_clamps(self):
        snapshot = claude_snapshot({
            "primary": {"remainingPercent": 120, "resetsAt": None},
            "weekly": None,
            "models": {
                "Sonnet": {"remainingPercent": 40, "resetsAt": 1.0},
                "Haiku": {"remainingPercent": 10, "resetsAt": 1.0},
            },
        })
        self.assertEqual(snapshot["primary"]["remainingPercent"], 100)
        self.assertEqual(list(snapshot["models"]), ["Sonnet"])

    def test_claude_snapshot_without_quota_is_an_error(self):
        with self.assertRaises(ValueError):
            claude_snapshot({"primary": None, "weekly": None, "models": {}})


class FormattingTests(unittest.TestCase):
    def test_epoch_accepts_numbers_and_iso_strings(self):
        self.assertEqual(epoch(5), 5.0)
        self.assertEqual(epoch("2026-01-01T00:00:00Z"), 1767225600.0)
        self.assertIsNone(epoch("not a date"))
        self.assertIsNone(epoch(None))

    def test_percent_rounds_and_clamps(self):
        self.assertEqual(percent(99.6), "100%")
        self.assertEqual(percent(-5), "0%")
        self.assertEqual(percent(None), "—")

    def test_safe_failure_never_leaks_exception_messages(self):
        self.assertEqual(safe_failure("Codex", TimeoutError("secret")), "query timed out")
        self.assertEqual(safe_failure("Claude", FileNotFoundError("C:/secret")), "CLI not found")
        self.assertEqual(safe_failure("Claude", RuntimeError("x exited with status 3")), "CLI exited (3)")
        self.assertEqual(safe_failure("Claude", RuntimeError("token abc")), "local quota service unavailable")
        self.assertEqual(safe_failure("Codex", KeyError("token")), "request failed (KeyError)")


if __name__ == "__main__":
    unittest.main()
