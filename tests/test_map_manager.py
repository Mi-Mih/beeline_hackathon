from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.map_manager import MapManagerError, MapStore


class MapStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MapStore(Path(self.temporary.name) / "routing.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_initial_status_and_single_active_job(self) -> None:
        self.assertEqual(self.store.status()["state"], "uninitialized")
        first = self.store.create_job("manual_download")
        second = self.store.create_job("schedule")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.status()["state"], "updating")

    def test_schedule_is_validated_and_persisted(self) -> None:
        schedule = self.store.set_schedule({"enabled": True, "interval_days": 7, "local_time": "02:30"})
        self.assertEqual(schedule["timezone"], "Europe/Moscow")
        self.assertTrue(self.store.status()["schedule"]["enabled"])
        with self.assertRaises(MapManagerError):
            self.store.set_schedule({"enabled": True, "interval_days": 0, "local_time": "02:30"})

    def test_cancel_transitions_job(self) -> None:
        job = self.store.create_job("manual_download")
        cancelled = self.store.cancel(job["id"])
        self.assertEqual(cancelled["status"], "cancelling")


if __name__ == "__main__":
    unittest.main()
