#!/usr/bin/env python3
"""Send deterministic timing edge cases to a locally enabled Tarmo server."""

from __future__ import annotations

import argparse
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def post(base_url: str, path: str, body: dict, expected: int = 200) -> dict:
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            status = response.status
            result = json.loads(response.read())
    except HTTPError as exc:
        status = exc.code
        result = json.loads(exc.read())
    if status != expected:
        raise RuntimeError(f"{path} returned {status}, expected {expected}: {result}")
    return result


class Emulator:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.run_id = str(time.time_ns())
        self.sequence = 0

    def event(
        self, device_time_us: int, *, boot_id: str = "emulator-boot-a",
        event_type: str = "crossing", **fields: object,
    ) -> dict:
        self.sequence += 1
        return {
            "event_id": f"emulator-{self.run_id}-{self.sequence}",
            "device_id": "tarmo-emulator",
            "boot_id": boot_id,
            "sequence": self.sequence,
            "type": event_type,
            "device_time_us": device_time_us,
            **fields,
        }

    def send(self, event: dict, transport: str, expected: int = 200) -> dict:
        return post(
            self.base_url, "/api/emulator/events",
            {"transport": transport, "event": event}, expected,
        )

    def arm(self, name: str, expected: int = 201) -> dict:
        return post(self.base_url, "/api/races", {"player_name": name}, expected)

    def cancel(self) -> None:
        post(self.base_url, "/api/races/cancel", {})

    def run(self) -> None:
        print("1/6 crossing with no active race")
        self.send(self.event(1_000_000, sensor_a_mm=200, sensor_b_mm=205), "wifi")

        print("2/6 firmware sensor activation and rejection telemetry")
        self.send(self.event(
            1_100_000, event_type="log", level="info", code="sensor_activated",
            message="Sensor A entered detection range", sensor="A", distance_mm=180,
            range_status=0,
        ), "usb")
        self.send(self.event(
            1_230_000, event_type="log", level="warning", code="sensor_single_rejected",
            message="Sensor A activation expired without Sensor B", sensor="A",
            distance_mm=180, range_status=0,
        ), "usb")

        print("3/6 normal race with USB-first/Wi-Fi-duplicate, then reverse")
        self.arm("Emulator Normal")
        start = self.event(2_000_000, sensor_a_mm=210, sensor_b_mm=215)
        self.send(start, "usb")
        duplicate = self.send(start, "wifi")
        if not duplicate.get("duplicate"):
            raise RuntimeError("Second transport was not identified as a duplicate")
        finish = self.event(14_345_678, sensor_a_mm=205, sensor_b_mm=209)
        self.send(finish, "wifi")
        self.send(finish, "usb")

        print("4/6 ESP32 restart between crossings")
        self.arm("Emulator Restart")
        self.send(self.event(20_000_000, boot_id="boot-before-restart"), "usb")
        restarted = self.send(self.event(1_000_000, boot_id="boot-after-restart"), "wifi")
        if restarted.get("transition") != "error":
            raise RuntimeError("Restart did not mark the race as an error")

        print("5/6 non-monotonic timestamp rejection")
        self.arm("Emulator Timestamp")
        self.send(self.event(30_000_000), "usb")
        self.send(self.event(29_000_000), "wifi", expected=400)
        self.cancel()

        print("6/6 malformed payload rejection")
        self.send({"type": "crossing"}, "usb", expected=400)
        print("Done. Inspect the operator audit log and filter by Sensors, USB, Wi-Fi, Race, and Errors.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    Emulator(args.server).run()


if __name__ == "__main__":
    main()
