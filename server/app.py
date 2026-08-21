#!/usr/bin/env python3
"""Local race server, persistent leaderboard, and USB serial bridge."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import termios
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SERVER_DIR = Path(__file__).resolve().parent
STATIC_DIR = SERVER_DIR / "static"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RaceError(ValueError):
    pass


class RaceStore:
    def __init__(self, database_path: Path, audit_log_limit: int = 100_000):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_limit = max(1_000, int(audit_log_limit))
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS races (
                    id INTEGER PRIMARY KEY,
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    status TEXT NOT NULL CHECK(status IN ('armed', 'running', 'complete', 'cancelled', 'error')),
                    armed_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    start_device_us INTEGER,
                    finish_device_us INTEGER,
                    elapsed_us INTEGER,
                    boot_id TEXT,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS device_events (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    device_time_us INTEGER NOT NULL,
                    transport TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL CHECK(level IN ('debug', 'info', 'warning', 'error')),
                    source TEXT NOT NULL,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    transport TEXT,
                    event_id TEXT,
                    race_id INTEGER,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_races_status ON races(status);
                CREATE INDEX IF NOT EXISTS idx_races_elapsed ON races(elapsed_us) WHERE status = 'complete';
                CREATE INDEX IF NOT EXISTS idx_device_events_received ON device_events(received_at);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_code ON audit_logs(code);
                PRAGMA optimize;
                """
            )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def _append_log(
        self, level: str, source: str, code: str, message: str, *,
        transport: str | None = None, event_id: str | None = None,
        race_id: int | None = None, details: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO audit_logs(
                created_at, level, source, code, message, transport,
                event_id, race_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(), level, source, code, message, transport, event_id,
                race_id, json.dumps(details or {}, separators=(",", ":")),
            ),
        )
        if cursor.lastrowid % 1000 == 0:
            self.connection.execute(
                """
                DELETE FROM audit_logs WHERE id IN (
                    SELECT id FROM audit_logs ORDER BY id DESC LIMIT -1 OFFSET ?
                )
                """,
                (self.audit_log_limit,),
            )
        return int(cursor.lastrowid)

    def record_log(
        self, level: str, source: str, code: str, message: str, **metadata: Any,
    ) -> int:
        with self.lock, self.connection:
            return self._append_log(level, source, code, message, **metadata)

    def logs(self, limit: int = 200, before_id: int | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        query = """
            SELECT id, created_at, level, source, code, message, transport,
                   event_id, race_id, details_json
            FROM audit_logs
        """
        parameters: list[Any] = []
        if before_id is not None:
            query += " WHERE id < ?"
            parameters.append(int(before_id))
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(limit)
        with self.lock:
            rows = self.connection.execute(query, parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def arm_race(self, player_name: str) -> dict[str, Any]:
        name = " ".join(player_name.strip().split())
        if not name:
            raise RaceError("Player name is required")
        if len(name) > 80:
            raise RaceError("Player name must be 80 characters or fewer")
        now = utc_now()
        with self.lock, self.connection:
            active = self.connection.execute(
                "SELECT id FROM races WHERE status IN ('armed', 'running') LIMIT 1"
            ).fetchone()
            if active:
                raise RaceError("Finish or cancel the active race before arming another")
            player_id = self.connection.execute(
                "INSERT INTO players(name, created_at) VALUES (?, ?)", (name, now)
            ).lastrowid
            race_id = self.connection.execute(
                "INSERT INTO races(player_id, status, armed_at) VALUES (?, 'armed', ?)",
                (player_id, now),
            ).lastrowid
            self._append_log(
                "info", "operator", "race_armed", f"Race armed for {name}",
                race_id=race_id, details={"player_name": name},
            )
        return self.get_race(race_id)

    def cancel_active(self) -> dict[str, Any] | None:
        now = utc_now()
        with self.lock, self.connection:
            active = self._active_row()
            if not active:
                return None
            self.connection.execute(
                "UPDATE races SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (now, active["id"]),
            )
            self._append_log(
                "warning", "operator", "race_cancelled",
                f"Race cancelled for {active['player_name']}", race_id=active["id"],
            )
        return self.get_race(active["id"])

    def ingest_event(self, event: dict[str, Any], transport: str) -> dict[str, Any]:
        if transport not in {"usb", "wifi", "emulator-usb", "emulator-wifi"}:
            raise RaceError("Unsupported event transport")
        try:
            clean = self._validate_event(event)
        except RaceError as exc:
            self.record_log(
                "error", "server", "event_rejected", str(exc), transport=transport,
                details={"payload_preview": repr(event)[:500]},
            )
            raise
        received_at = utc_now()
        payload_json = json.dumps(clean, separators=(",", ":"))
        try:
            with self.lock, self.connection:
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO device_events(
                        event_id, device_id, boot_id, sequence, event_type,
                        device_time_us, transport, received_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean["event_id"], clean["device_id"], clean["boot_id"],
                        clean["sequence"], clean["type"], clean["device_time_us"],
                        transport, received_at, payload_json,
                    ),
                )
                if cursor.rowcount == 0:
                    existing = self.connection.execute(
                        "SELECT payload_json FROM device_events WHERE event_id = ?",
                        (clean["event_id"],),
                    ).fetchone()
                    if existing and existing["payload_json"] != payload_json:
                        self._append_log(
                            "error", "transport", "event_id_conflict",
                            "Event ID was reused with different payload contents",
                            transport=transport, event_id=clean["event_id"],
                            details={"device_id": clean["device_id"], "sequence": clean["sequence"]},
                        )
                        return {
                            "accepted": False, "duplicate": False, "conflict": True,
                            "race_changed": False,
                        }
                    self._append_log(
                        "info", "transport", "event_duplicate",
                        f"Duplicate {clean['type']} received over {transport}",
                        transport=transport, event_id=clean["event_id"],
                        details={"device_id": clean["device_id"], "sequence": clean["sequence"]},
                    )
                    return {"accepted": True, "duplicate": True, "race_changed": False}

                self._append_log(
                    "info", "transport", "event_accepted",
                    f"New {clean['type']} accepted over {transport}",
                    transport=transport, event_id=clean["event_id"],
                    details={"device_id": clean["device_id"], "sequence": clean["sequence"]},
                )

                if clean["type"] == "log":
                    self._append_log(
                        clean.get("level", "info"), "firmware",
                        clean.get("code", "firmware_log"),
                        clean.get("message", "Firmware telemetry"),
                        transport=transport, event_id=clean["event_id"],
                        details={key: value for key, value in clean.items() if key not in {
                            "event_id", "device_id", "boot_id", "sequence", "type",
                            "device_time_us", "level", "code", "message",
                        }},
                    )
                    return {"accepted": True, "duplicate": False, "race_changed": False}

                active = self._active_row()
                if clean["type"] != "crossing":
                    return {"accepted": True, "duplicate": False, "race_changed": False}
                if not active:
                    self._append_log(
                        "warning", "race", "crossing_ignored_no_active_race",
                        "Crossing stored but ignored because no race is armed",
                        transport=transport, event_id=clean["event_id"],
                    )
                    return {"accepted": True, "duplicate": False, "race_changed": False}

                race_id = active["id"]
                if active["status"] == "armed":
                    self.connection.execute(
                        """
                        UPDATE races SET status = 'running', started_at = ?,
                            start_device_us = ?, boot_id = ? WHERE id = ?
                        """,
                        (received_at, clean["device_time_us"], clean["boot_id"], race_id),
                    )
                    self._append_log(
                        "info", "race", "race_started",
                        f"Lap started for {active['player_name']}", transport=transport,
                        event_id=clean["event_id"], race_id=race_id,
                        details={"start_device_us": clean["device_time_us"]},
                    )
                    return {
                        "accepted": True, "duplicate": False, "race_changed": True,
                        "transition": "started", "race": self.get_race(race_id),
                    }

                if clean["boot_id"] != active["boot_id"]:
                    message = "ESP32 restarted between the start and finish crossings"
                    self.connection.execute(
                        "UPDATE races SET status = 'error', finished_at = ?, error_message = ? WHERE id = ?",
                        (received_at, message, race_id),
                    )
                    self._append_log(
                        "error", "race", "race_failed_device_restart", message,
                        transport=transport, event_id=clean["event_id"], race_id=race_id,
                        details={"start_boot_id": active["boot_id"], "finish_boot_id": clean["boot_id"]},
                    )
                    return {
                        "accepted": True, "duplicate": False, "race_changed": True,
                        "transition": "error", "race": self.get_race(race_id),
                    }

                elapsed_us = clean["device_time_us"] - active["start_device_us"]
                if elapsed_us <= 0:
                    raise RaceError("Finish timestamp must be after the start timestamp")
                self.connection.execute(
                    """
                    UPDATE races SET status = 'complete', finished_at = ?,
                        finish_device_us = ?, elapsed_us = ? WHERE id = ?
                    """,
                    (received_at, clean["device_time_us"], elapsed_us, race_id),
                )
                self._append_log(
                    "info", "race", "race_finished",
                    f"Lap finished for {active['player_name']}", transport=transport,
                    event_id=clean["event_id"], race_id=race_id,
                    details={"finish_device_us": clean["device_time_us"], "elapsed_us": elapsed_us},
                )
                return {
                    "accepted": True, "duplicate": False, "race_changed": True,
                    "transition": "finished", "race": self.get_race(race_id),
                }
        except RaceError as exc:
            self.record_log(
                "error", "server", "event_rejected", str(exc), transport=transport,
                event_id=clean.get("event_id"), details={"payload_preview": repr(clean)[:500]},
            )
            raise

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
        required = ("event_id", "device_id", "boot_id", "sequence", "type", "device_time_us")
        missing = [key for key in required if key not in event]
        if missing:
            raise RaceError(f"Missing event fields: {', '.join(missing)}")
        clean = dict(event)
        for key in ("event_id", "device_id", "boot_id", "type"):
            clean[key] = str(clean[key]).strip()
            if not clean[key] or len(clean[key]) > 96:
                raise RaceError(f"Invalid {key}")
        if clean["type"] not in {"crossing", "heartbeat", "log"}:
            raise RaceError("Unsupported event type")
        if clean["type"] == "log":
            clean["level"] = str(clean.get("level", "info"))
            if clean["level"] not in {"debug", "info", "warning", "error"}:
                raise RaceError("Invalid log level")
            clean["code"] = str(clean.get("code", "firmware_log"))[:96]
            clean["message"] = str(clean.get("message", "Firmware telemetry"))[:500]
        try:
            clean["sequence"] = int(clean["sequence"])
            clean["device_time_us"] = int(clean["device_time_us"])
        except (TypeError, ValueError) as exc:
            raise RaceError("sequence and device_time_us must be integers") from exc
        if clean["sequence"] < 0 or clean["device_time_us"] < 0:
            raise RaceError("sequence and device_time_us cannot be negative")
        return clean

    def _active_row(self) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT races.*, players.name AS player_name
            FROM races JOIN players ON players.id = races.player_id
            WHERE races.status IN ('armed', 'running')
            ORDER BY races.id DESC LIMIT 1
            """
        ).fetchone()

    def get_race(self, race_id: int) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT races.*, players.name AS player_name
                FROM races JOIN players ON players.id = races.player_id
                WHERE races.id = ?
                """,
                (race_id,),
            ).fetchone()
        if not row:
            raise RaceError("Race not found")
        return dict(row)

    def state(self) -> dict[str, Any]:
        with self.lock:
            active = self._active_row()
            leaders = self.connection.execute(
                """
                SELECT races.id, players.name AS player_name, races.elapsed_us,
                       races.finished_at
                FROM races JOIN players ON players.id = races.player_id
                WHERE races.status = 'complete'
                ORDER BY races.elapsed_us ASC, races.finished_at ASC LIMIT 100
                """
            ).fetchall()
            recent = self.connection.execute(
                """
                SELECT races.id, players.name AS player_name, races.status,
                       races.elapsed_us, races.armed_at, races.finished_at, races.error_message
                FROM races JOIN players ON players.id = races.player_id
                ORDER BY races.id DESC LIMIT 12
                """
            ).fetchall()
            device = self.connection.execute(
                """
                SELECT device_id, received_at, transport FROM device_events
                ORDER BY received_at DESC LIMIT 1
                """
            ).fetchone()
        return {
            "active_race": dict(active) if active else None,
            "leaderboard": [dict(row) for row in leaders],
            "recent_races": [dict(row) for row in recent],
            "device": dict(device) if device else None,
            "server_time": utc_now(),
        }


class EventBus:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.version = 0

    def publish(self) -> None:
        with self.condition:
            self.version += 1
            self.condition.notify_all()

    def wait(self, last_version: int, timeout: float = 15.0) -> int:
        with self.condition:
            if self.version == last_version:
                self.condition.wait(timeout)
            return self.version


@dataclass
class Runtime:
    store: RaceStore
    bus: EventBus
    emulator_enabled: bool = False


class RaceRequestHandler(BaseHTTPRequestHandler):
    runtime: Runtime
    server_version = "TarmoRace/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/leaderboard")
            self.end_headers()
        elif path == "/api/state":
            self._send_json(self.runtime.store.state())
        elif path == "/api/events":
            self._serve_events()
        elif path == "/api/logs":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["200"])[0])
                before = query.get("before_id", [None])[0]
                before_id = int(before) if before is not None else None
            except ValueError:
                self._send_json(
                    {"error": "limit and before_id must be integers"},
                    HTTPStatus.BAD_REQUEST,
                )
            else:
                self._send_json({"logs": self.runtime.store.logs(limit, before_id)})
        elif path == "/health":
            self._send_json({"ok": True})
        elif path == "/operator":
            self._serve_file("operator.html")
        elif path == "/leaderboard":
            self._serve_file("leaderboard.html")
        elif path.startswith("/static/"):
            self._serve_file(path.removeprefix("/static/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/races":
                race = self.runtime.store.arm_race(str(body.get("player_name", "")))
                self.runtime.bus.publish()
                self._send_json({"race": race}, HTTPStatus.CREATED)
            elif path == "/api/races/cancel":
                race = self.runtime.store.cancel_active()
                self.runtime.bus.publish()
                self._send_json({"race": race})
            elif path == "/api/device/events":
                result = self.runtime.store.ingest_event(body, "wifi")
                self.runtime.bus.publish()
                self._send_json(result)
            elif path == "/api/emulator/events":
                if not self.runtime.emulator_enabled:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                requested_transport = str(body.get("transport", "wifi"))
                if requested_transport not in {"usb", "wifi"}:
                    raise RaceError("Emulator transport must be usb or wifi")
                event = body.get("event")
                if not isinstance(event, dict):
                    raise RaceError("Emulator event must be a JSON object")
                result = self.runtime.store.ingest_event(
                    event, f"emulator-{requested_transport}",
                )
                self.runtime.bus.publish()
                self._send_json(result)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (RaceError, json.JSONDecodeError) as exc:
            self.runtime.store.record_log(
                "warning", "http", "request_rejected", str(exc),
                transport="wifi" if path == "/api/device/events" else None,
                details={"path": path, "client": self.client_address[0]},
            )
            self.runtime.bus.publish()
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            print(f"Request failed: {exc}", file=sys.stderr)
            self._send_json({"error": "Internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16_384:
            raise RaceError("A small JSON request body is required")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise RaceError("JSON body must be an object")
        return value

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, relative_name: str) -> None:
        candidate = (STATIC_DIR / relative_name).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
        }
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types.get(candidate.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        version = -1
        try:
            while True:
                next_version = self.runtime.bus.wait(version)
                if next_version != version:
                    payload = json.dumps(self.runtime.store.state(), separators=(",", ":"))
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode())
                    version = next_version
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}")


class SerialBridge(threading.Thread):
    def __init__(self, runtime: Runtime, port_setting: str | None, baud: int):
        super().__init__(daemon=True, name="serial-bridge")
        self.runtime = runtime
        self.port_setting = port_setting
        self.baud = baud
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        if self.port_setting is None:
            print("USB serial ingestion disabled")
            self.runtime.store.record_log(
                "warning", "usb_bridge", "usb_disabled", "USB serial ingestion is disabled",
            )
            return
        while not self.stop_event.is_set():
            port = self._find_port()
            if not port:
                self.stop_event.wait(2.0)
                continue
            try:
                print(f"USB bridge connected to {port} at {self.baud} baud")
                self.runtime.store.record_log(
                    "info", "usb_bridge", "usb_connected",
                    f"USB bridge connected to {port}", transport="usb",
                    details={"port": port, "baud": self.baud},
                )
                self.runtime.bus.publish()
                self._read_port(port)
            except (OSError, termios.error) as exc:
                print(f"USB bridge disconnected from {port}: {exc}", file=sys.stderr)
                self.runtime.store.record_log(
                    "warning", "usb_bridge", "usb_disconnected",
                    f"USB bridge disconnected from {port}", transport="usb",
                    details={"port": port, "error": str(exc)},
                )
                self.runtime.bus.publish()
                self.stop_event.wait(1.0)

    def _find_port(self) -> str | None:
        if self.port_setting != "auto":
            return self.port_setting if Path(self.port_setting).exists() else None
        patterns = (
            "/dev/cu.usbmodem*", "/dev/cu.usbserial*",
            "/dev/ttyACM*", "/dev/ttyUSB*",
        )
        matches = [path for pattern in patterns for path in glob.glob(pattern)]
        return sorted(matches)[0] if matches else None

    def _read_port(self, port: str) -> None:
        descriptor = os.open(port, os.O_RDWR | os.O_NOCTTY)
        try:
            attributes = termios.tcgetattr(descriptor)
            speed = getattr(termios, f"B{self.baud}", termios.B115200)
            attributes[0] = 0
            attributes[1] = 0
            attributes[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attributes[3] = 0
            attributes[4] = speed
            attributes[5] = speed
            attributes[6][termios.VMIN] = 1
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
            buffer = bytearray()
            while not self.stop_event.is_set():
                chunk = os.read(descriptor, 256)
                if not chunk:
                    raise OSError("serial device closed")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw_line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    self._handle_line(raw_line.strip())
                if len(buffer) > 16_384:
                    self.runtime.store.record_log(
                        "error", "usb_bridge", "usb_buffer_overflow",
                        "Discarded an unterminated USB serial line larger than 16 KiB",
                        transport="usb", details={"bytes_discarded": len(buffer)},
                    )
                    self.runtime.bus.publish()
                    buffer.clear()
        finally:
            os.close(descriptor)

    def _handle_line(self, raw_line: bytes) -> None:
        if not raw_line.startswith(b"{"):
            message = raw_line.decode("utf-8", errors="replace")[:1000]
            print(f"ESP32: {message}")
            if message:
                level = "error" if message.startswith("ERROR") else "info"
                self.runtime.store.record_log(
                    level, "firmware", "firmware_console", message, transport="usb",
                )
                self.runtime.bus.publish()
            return
        try:
            event = json.loads(raw_line)
            result = self.runtime.store.ingest_event(event, "usb")
            self.runtime.bus.publish()
        except (json.JSONDecodeError, RaceError) as exc:
            print(f"Ignored invalid USB event: {exc}", file=sys.stderr)
            self.runtime.store.record_log(
                "error", "usb_bridge", "usb_payload_rejected",
                f"Ignored invalid USB event: {exc}", transport="usb",
                details={"payload_preview": raw_line.decode("utf-8", errors="replace")[:500]},
            )
            self.runtime.bus.publish()


def load_config(config_path: Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "host": "0.0.0.0",
        "port": 8080,
        "database_path": "data/races.db",
        "serial_port": "auto",
        "serial_baud": 115200,
        "emulator_enabled": False,
        "audit_log_limit": 100000,
    }
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RaceError("Server config must contain a JSON object")
        defaults.update(loaded)
    return defaults


def main() -> None:
    parser = argparse.ArgumentParser(description="Tarmo local race server")
    parser.add_argument("--config", type=Path, default=SERVER_DIR / "config.json")
    args = parser.parse_args()
    config = load_config(args.config)
    database_path = Path(config["database_path"])
    if not database_path.is_absolute():
        database_path = SERVER_DIR / database_path

    store = RaceStore(database_path, int(config.get("audit_log_limit", 100_000)))
    runtime = Runtime(
        store=store,
        bus=EventBus(),
        emulator_enabled=bool(config.get("emulator_enabled", False)),
    )
    RaceRequestHandler.runtime = runtime
    server = ThreadingHTTPServer((str(config["host"]), int(config["port"])), RaceRequestHandler)
    bridge = SerialBridge(runtime, config.get("serial_port"), int(config["serial_baud"]))
    store.record_log(
        "info", "server", "server_started",
        f"Server listening on {config['host']}:{config['port']}",
    )
    bridge.start()
    print(f"Tarmo Race is ready at http://localhost:{config['port']}")
    print(f"Operator: http://localhost:{config['port']}/operator")
    print(f"Leaderboard: http://localhost:{config['port']}/leaderboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        store.record_log("info", "server", "server_stopping", "Server stopping cleanly")
        bridge.stop()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
