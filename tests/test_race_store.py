import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "server"))

from app import RaceError, RaceStore  # noqa: E402


def crossing(sequence: int, device_time_us: int, boot_id: str = "abc123"):
    return {
        "event_id": f"gate-{boot_id}-{sequence}",
        "device_id": "gate",
        "boot_id": boot_id,
        "sequence": sequence,
        "type": "crossing",
        "device_time_us": device_time_us,
        "sensor_a_mm": 211,
        "sensor_b_mm": 224,
    }


class RaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RaceStore(Path(self.temp_dir.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_one_lap_flow_uses_device_timestamps(self):
        armed = self.store.arm_race("  Alex   Morgan ")
        self.assertEqual(armed["player_name"], "Alex Morgan")
        self.assertEqual(armed["status"], "armed")

        started = self.store.ingest_event(crossing(1, 2_000_000), "usb")
        self.assertEqual(started["transition"], "started")

        finished = self.store.ingest_event(crossing(2, 14_345_678), "wifi")
        self.assertEqual(finished["transition"], "finished")
        self.assertEqual(finished["race"]["elapsed_us"], 12_345_678)
        self.assertEqual(self.store.state()["leaderboard"][0]["player_name"], "Alex Morgan")

    def test_wifi_and_usb_duplicates_do_not_advance_race_twice(self):
        self.store.arm_race("Sam")
        event = crossing(1, 4_000_000)
        first = self.store.ingest_event(event, "usb")
        duplicate = self.store.ingest_event(event, "wifi")
        self.assertTrue(first["race_changed"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.store.state()["active_race"]["status"], "running")

    def test_fastest_lap_is_ranked_first(self):
        self.store.arm_race("First")
        self.store.ingest_event(crossing(1, 1_000_000), "usb")
        self.store.ingest_event(crossing(2, 12_000_000), "usb")
        self.store.arm_race("Second")
        self.store.ingest_event(crossing(3, 20_000_000), "usb")
        self.store.ingest_event(crossing(4, 29_000_000), "usb")
        leaders = self.store.state()["leaderboard"]
        self.assertEqual([row["player_name"] for row in leaders], ["Second", "First"])

    def test_restart_between_crossings_marks_timing_error(self):
        self.store.arm_race("Taylor")
        self.store.ingest_event(crossing(1, 1_000_000, "boot-a"), "usb")
        result = self.store.ingest_event(crossing(1, 2_000_000, "boot-b"), "usb")
        self.assertEqual(result["transition"], "error")
        self.assertIsNone(self.store.state()["active_race"])

    def test_only_one_race_can_be_active(self):
        self.store.arm_race("Jordan")
        with self.assertRaises(RaceError):
            self.store.arm_race("Casey")

    def test_invalid_event_is_rejected(self):
        self.store.arm_race("Jamie")
        with self.assertRaises(RaceError):
            self.store.ingest_event({"type": "crossing"}, "usb")


if __name__ == "__main__":
    unittest.main()
