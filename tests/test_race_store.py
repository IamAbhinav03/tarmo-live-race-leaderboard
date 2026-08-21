import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "server"))

from app import EventBus, RaceError, RaceStore, Runtime, SerialBridge  # noqa: E402


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
        transport_logs = [
            row for row in self.store.logs()
            if row["event_id"] == event["event_id"] and row["source"] == "transport"
        ]
        self.assertEqual(
            [(row["code"], row["transport"]) for row in reversed(transport_logs)],
            [("event_accepted", "usb"), ("event_duplicate", "wifi")],
        )

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
        self.assertEqual(self.store.logs()[0]["code"], "event_rejected")
        self.assertEqual(self.store.logs()[0]["transport"], "usb")

    def test_crossing_without_active_race_is_stored_and_audited(self):
        result = self.store.ingest_event(crossing(1, 1_000_000), "wifi")
        self.assertFalse(result["race_changed"])
        self.assertEqual(self.store.logs()[0]["code"], "crossing_ignored_no_active_race")

    def test_firmware_sensor_telemetry_is_structured(self):
        event = {
            **crossing(7, 7_000_000),
            "type": "log",
            "level": "info",
            "code": "sensor_activated",
            "message": "Sensor A entered detection range",
            "sensor": "A",
            "distance_mm": 188,
            "range_status": 0,
        }
        result = self.store.ingest_event(event, "usb")
        self.assertTrue(result["accepted"])
        firmware_log = next(row for row in self.store.logs() if row["source"] == "firmware")
        self.assertEqual(firmware_log["code"], "sensor_activated")
        self.assertEqual(firmware_log["details"]["sensor"], "A")
        self.assertEqual(firmware_log["details"]["distance_mm"], 188)

    def test_reused_event_id_with_different_payload_is_a_conflict(self):
        self.store.arm_race("Conflict")
        original = crossing(1, 1_000_000)
        self.store.ingest_event(original, "usb")
        altered = {**original, "device_time_us": 9_000_000}
        result = self.store.ingest_event(altered, "wifi")
        self.assertFalse(result["accepted"])
        self.assertTrue(result["conflict"])
        self.assertEqual(self.store.logs()[0]["code"], "event_id_conflict")
        self.assertEqual(self.store.state()["active_race"]["status"], "running")

    def test_restart_error_and_timestamp_rejection_are_audited(self):
        self.store.arm_race("Restart")
        self.store.ingest_event(crossing(1, 5_000_000, "boot-a"), "usb")
        self.store.ingest_event(crossing(2, 1_000_000, "boot-b"), "wifi")
        self.assertEqual(self.store.logs()[0]["code"], "race_failed_device_restart")

        self.store.arm_race("Rollback")
        self.store.ingest_event(crossing(3, 8_000_000, "boot-c"), "usb")
        with self.assertRaises(RaceError):
            self.store.ingest_event(crossing(4, 7_000_000, "boot-c"), "wifi")
        self.assertEqual(self.store.logs()[0]["code"], "event_rejected")

    def test_audit_logs_persist_after_reopening_database(self):
        database_path = Path(self.temp_dir.name) / "persistent.db"
        first = RaceStore(database_path)
        first.record_log("info", "test", "persistent", "Stored on disk")
        first.close()
        second = RaceStore(database_path)
        try:
            self.assertEqual(second.logs()[0]["code"], "persistent")
        finally:
            second.close()

    def test_sensor_logs_do_not_publish_race_state(self):
        bus = EventBus()
        bridge = SerialBridge(Runtime(store=self.store, bus=bus), None, 115200)
        sensor_log = {
            **crossing(20, 20_000_000),
            "type": "log",
            "level": "info",
            "code": "sensor_activated",
            "message": "Sensor A entered detection range",
            "sensor": "A",
            "distance_mm": 175,
            "range_status": 0,
        }
        bridge._handle_line(json.dumps(sensor_log).encode())
        bridge._handle_line(b"DIAG random sensor noise")
        self.assertEqual(bus.version, 0)
        self.assertEqual(self.store.logs()[0]["code"], "firmware_console")

    def test_crossing_publishes_only_when_race_changes(self):
        bus = EventBus()
        bridge = SerialBridge(Runtime(store=self.store, bus=bus), None, 115200)
        ignored = crossing(30, 30_000_000)
        bridge._handle_line(json.dumps(ignored).encode())
        self.assertEqual(bus.version, 0)

        self.store.arm_race("Broadcast")
        start = crossing(31, 31_000_000)
        encoded = json.dumps(start).encode()
        bridge._handle_line(encoded)
        self.assertEqual(bus.version, 1)
        bridge._handle_line(encoded)
        self.assertEqual(bus.version, 1)

    def test_cannon_clash_converts_units_and_ranks_personal_bests(self):
        first = self.store.record_cannon_result("  Alex   Morgan ", "10", "ft")
        self.assertEqual(first["player_name"], "Alex Morgan")
        self.assertEqual(first["distance_mm"], 3048)
        self.store.record_cannon_result("alex morgan", "4", "m")
        self.store.record_cannon_result("Casey", "350", "cm")

        state = self.store.state()
        self.assertEqual(
            [(row["player_name"], row["distance_mm"]) for row in state["cannon_leaderboard"]],
            [("alex morgan", 4000), ("Casey", 3500)],
        )
        self.assertEqual(len(state["recent_cannon"]), 3)
        self.assertEqual(self.store.logs()[0]["code"], "cannon_result_recorded")

    def test_cannon_clash_rejects_invalid_measurements(self):
        for distance, unit in (
            ("0", "m"), ("not-a-number", "m"), ("5", "yards"),
            ("0.49", "mm"), ("100000000.5", "mm"),
        ):
            with self.subTest(distance=distance, unit=unit):
                with self.assertRaises(RaceError):
                    self.store.record_cannon_result("Jordan", distance, unit)


if __name__ == "__main__":
    unittest.main()
