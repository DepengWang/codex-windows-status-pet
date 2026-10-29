import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.quota_format_api import (
    earliest_future_expiry,
    quota_line,
    reset_credit_line,
    reset_display_time,
)


class QuotaFormatTests(unittest.TestCase):
    def test_earliest_future_expiry_ignores_invalid_and_past_values(self):
        self.assertEqual(
            earliest_future_expiry([{"expiresAt": "90"}, {"expiresAt": "120"}, {"expiresAt": "bad"}], now=100),
            120.0,
        )

    def test_earliest_expiry_accepts_iso_reset_field(self):
        self.assertEqual(
            earliest_future_expiry({"items": [{"resetsAt": "1970-01-01T00:02:00Z"}]}, now=100),
            120.0,
        )

    def test_dates_have_no_leading_zero(self):
        timestamp = datetime(2026, 7, 10, 15, 16).astimezone().timestamp()
        with patch("api.quota_format_api.datetime") as mocked:
            mocked.fromtimestamp.side_effect = datetime.fromtimestamp
            mocked.now.side_effect = datetime.now
            line = quota_line("周", "85%", timestamp)
        self.assertRegex(line, r"^周 85% / 15:16 7/10$")

    def test_missing_reset_time_is_not_invented(self):
        self.assertEqual(quota_line("Weekly", "85%", None), "Weekly 85%")
        self.assertEqual(reset_credit_line(5, None), "重置 5 次")

    def test_reset_today_omits_date(self):
        now = datetime(2026, 10, 29, 9, 0).astimezone()
        reset = now.replace(hour=21, minute=22, second=0, microsecond=0)
        self.assertEqual(reset_display_time(reset.timestamp(), now=now), "21:22")

    def test_reset_on_another_day_includes_month_and_day(self):
        now = datetime(2026, 10, 29, 9, 0).astimezone()
        reset = now + timedelta(days=2)
        self.assertEqual(
            reset_display_time(reset.timestamp(), now=now),
            f"{reset:%H:%M} {reset.month}/{reset.day}",
        )


if __name__ == "__main__":
    unittest.main()
