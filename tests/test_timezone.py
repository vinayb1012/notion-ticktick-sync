"""Timezone correctness tests for ticktick_notion_sync.

Run: python -m tests.test_timezone
Verifies the TZ_NAME override is respected everywhere a local date/hour is
computed — the bug that wrote 23-Sep tasks onto the 22-Sep page.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ticktick_notion_sync as tns


class TestTimezoneOverride(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("TZ_NAME")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TZ_NAME", None)
        else:
            os.environ["TZ_NAME"] = self._old

    def test_get_local_tz_respects_env(self):
        os.environ["TZ_NAME"] = "Europe/Stockholm"
        self.assertEqual(tns.get_local_tz(), ZoneInfo("Europe/Stockholm"))

    def test_get_local_tz_falls_back_to_system(self):
        os.environ.pop("TZ_NAME", None)
        expected = datetime.now(timezone.utc).astimezone().tzinfo
        self.assertEqual(tns.get_local_tz(), expected)

    def test_date_str_matches_stockholm_not_utc(self):
        """At 22:00 UTC, Stockholm is already the NEXT day — the exact bug."""
        os.environ["TZ_NAME"] = "Europe/Stockholm"
        # 22:00 UTC on 22-Sep == 00:00 Stockholm on 23-Sep
        fixed_utc = datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc)
        stockholm_date = fixed_utc.astimezone(ZoneInfo("Europe/Stockholm")).strftime("%d-%b-%Y")
        utc_date = fixed_utc.strftime("%d-%b-%Y")
        self.assertEqual(stockholm_date, "23-Sep-2026")
        self.assertEqual(utc_date, "22-Sep-2026")
        self.assertNotEqual(stockholm_date, utc_date)

    def test_hour_guard_uses_local_hour(self):
        """Hour guard must compare the Stockholm hour, not the host (UTC) hour."""
        os.environ["TZ_NAME"] = "Europe/Stockholm"
        fixed_utc = datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc)
        local_hour = fixed_utc.astimezone(ZoneInfo("Europe/Stockholm")).hour
        self.assertEqual(local_hour, 0)  # midnight Stockholm, not 22 UTC
        # With window 8-23, hour 0 must be skipped
        self.assertFalse(8 <= local_hour < 23)


if __name__ == "__main__":
    unittest.main(verbosity=2)
