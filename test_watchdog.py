"""Watchdog BLIND check — the 2026-08-31..09-22 expired-login outage."""
import json
import os
import unittest
from unittest import mock

import watchdog


class TestBlindCheck(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "logs"))
        self.p = [mock.patch.object(watchdog, "ROOT", self.tmp),
                  mock.patch.object(watchdog, "STATE",
                                    os.path.join(self.tmp, "logs", "wd.json")),
                  mock.patch.object(watchdog, "webhook_url", lambda: None)]
        for x in self.p:
            x.start()
        self.alerts = []
        self.p2 = mock.patch.object(watchdog, "alert",
                                    lambda k, s, b: self.alerts.append(k))
        self.p2.start()

    def tearDown(self):
        self.p2.stop()
        for x in self.p:
            x.stop()

    def _write(self, name, obj):
        with open(os.path.join(self.tmp, "logs", name), "w") as f:
            json.dump(obj, f)

    def test_streak_alerts_after_three_failed_reads(self):
        self._write("cycle_status.json", {"detail": "broker-read-failed"})
        self.assertEqual(watchdog.blind_check(), 1)
        self.assertEqual(watchdog.blind_check(), 2)
        self.assertNotIn("blind", self.alerts)
        self.assertEqual(watchdog.blind_check(), 3)
        self.assertIn("blind", self.alerts)

    def test_success_resets_streak(self):
        self._write("cycle_status.json", {"detail": "broker-read-failed"})
        watchdog.blind_check()
        watchdog.blind_check()
        self._write("cycle_status.json", {"detail": "ok"})
        self.assertEqual(watchdog.blind_check(), 0)
        self.assertEqual(self.alerts, [])

    def test_failed_preflight_alerts(self):
        self._write("preflight.json", {"healthy": False, "detail": "OAuth expired"})
        watchdog.blind_check()
        self.assertIn("preflight", self.alerts)

    def test_healthy_preflight_is_quiet(self):
        self._write("preflight.json", {"healthy": True})
        watchdog.blind_check()
        self.assertEqual(self.alerts, [])


if __name__ == "__main__":
    unittest.main()
