import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.claude_usage_api import parse_claude_usage


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 29, 10, 0).astimezone()

    def test_session_and_week_percentages_are_converted_to_remaining(self):
        result = parse_claude_usage(
            "Current session: 34% used · resets Oct 29, 2:59pm (Asia/Shanghai)\n"
            "Current week (all models): 5% used · resets Nov 5, 2:59am (Asia/Shanghai)",
            now=self.now,
        )

        self.assertEqual(result["primary"]["remainingPercent"], 66)
        self.assertEqual(result["weekly"]["remainingPercent"], 95)
        self.assertEqual(result["models"], {})
        self.assertEqual(
            datetime.fromtimestamp(result["primary"]["resetsAt"]).astimezone().strftime("%H:%M"),
            "14:59",
        )
        weekly_reset = datetime.fromtimestamp(result["weekly"]["resetsAt"]).astimezone()
        self.assertEqual(f"{weekly_reset.month}/{weekly_reset.day}", "11/5")

    def test_sonnet_and_opus_weekly_windows_are_extracted_without_raw_text(self):
        result = parse_claude_usage(
            "Current week (all models): 10% used · resets Nov 5, 2:59am (Asia/Shanghai)\n"
            "Current week (Sonnet only): 60% used · resets Nov 5, 2:59am (Asia/Shanghai)\n"
            "Current week (Opus only): 25% used · resets Nov 5, 2:59am (Asia/Shanghai)\n"
            "Current session cost: $0.03; prompt text must not be retained",
            now=self.now,
        )

        self.assertEqual(result["weekly"]["remainingPercent"], 90)
        self.assertEqual(result["models"]["Sonnet"]["remainingPercent"], 40)
        self.assertEqual(result["models"]["Opus"]["remainingPercent"], 75)
        self.assertNotIn("prompt", repr(result).lower())
        self.assertNotIn("cost", repr(result).lower())

    def test_missing_or_invalid_quota_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "quota windows"):
            parse_claude_usage("No quota data is available", now=self.now)

        with self.assertRaisesRegex(ValueError, "quota windows"):
            parse_claude_usage(
                "Current week (all models): unknown · resets Nov 5, 2:59am",
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
