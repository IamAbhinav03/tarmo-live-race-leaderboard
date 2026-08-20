# Tarmo Live Race Leaderboard

A local, fault-tolerant lap timer for a one-car, one-lap race. Two VL53L0X sensors must agree on a crossing before the ESP32-C3 emits an event. Every event is sent over Wi-Fi and USB serial; the track-side server deduplicates the two copies, stores races in SQLite, and updates the operator console and public leaderboard live.

## How a race works

1. An operator opens `/operator`, enters the driver's name, and arms a race.
2. The first confirmed two-sensor crossing starts the timer.
3. The second confirmed crossing finishes the single lap.
4. The firmware's microsecond clock supplies the official elapsed time, so network or USB latency does not affect the result.
5. The result is persisted and appears immediately on `/leaderboard`, sorted fastest first.

## Project layout

```text
firmware/           ESP32-C3 PlatformIO/Arduino firmware
server/             Zero-dependency Python race server and USB bridge
server/static/      Operator and leaderboard web screens
tests/              Server and timing tests
```

## Hardware wiring

Both VL53L0X boards share SDA and SCL. Each needs a separate XSHUT connection so the firmware can give the identical sensors different I2C addresses.

| ESP32-C3 configuration | Sensor A | Sensor B |
|---|---|---|
| 3V3 | VIN/VCC | VIN/VCC |
| GND | GND | GND |
| `I2C_SDA_PIN` | SDA | SDA |
| `I2C_SCL_PIN` | SCL | SCL |
| `SENSOR_A_XSHUT_PIN` | XSHUT | — |
| `SENSOR_B_XSHUT_PIN` | — | XSHUT |

The example pin numbers target the generic `esp32-c3-devkitm-1` PlatformIO environment. Check the silkscreen and schematic for your exact board before powering it. Use 3.3 V logic and follow the voltage specification of your VL53L0X breakout board.

## Firmware setup

1. Install [PlatformIO](https://platformio.org/).
2. Copy `firmware/include/config.example.h` to `firmware/include/local_config.h`.
3. Set the Wi-Fi credentials, track-side server address, static IP details, and GPIO pins in `local_config.h`.
4. If your exact ESP32-C3 board is not an ESP32-C3-DevKitM-1, change `board` in `firmware/platformio.ini` to its PlatformIO board ID.
5. From `firmware/`, run `pio run --target upload`, then `pio device monitor`.

`local_config.h` is intentionally ignored by Git so credentials and site-specific network values are not published.

## Track-side server

Python 3.10 or newer is sufficient; no packages need to be installed.

```bash
cp server/config.example.json server/config.json
python3 server/app.py
```

Open:

- Operator console: `http://localhost:8080/operator`
- Live leaderboard: `http://localhost:8080/leaderboard`

The server automatically looks for `/dev/cu.usbmodem*` and `/dev/cu.usbserial*` on macOS and `/dev/ttyACM*` or `/dev/ttyUSB*` on Linux. To select a device explicitly, set `serial_port` in `server/config.json`. Set it to `null` to disable USB ingestion.

The SQLite database is stored at `server/data/races.db` by default. Back up that file to preserve race history.

## Reliability model

- A crossing is valid only when both sensors transition to a stable near reading inside `SENSOR_COINCIDENCE_MS`.
- Both sensors must clear before another crossing can be recorded.
- A post-crossing lockout rejects reflections and repeat samples from the same car.
- The ESP32 emits each event immediately over USB and queues it for Wi-Fi delivery.
- Both transports carry the same unique event ID. The server's database uniqueness constraint makes duplicate delivery harmless.
- Official lap time is the difference between the two ESP32 microsecond timestamps, not server arrival time.
- If the ESP32 restarts between crossings, the server refuses to calculate a lap across different boot IDs.

Two-sensor agreement substantially reduces false triggers, but physical placement still matters. Aim both sensors at the same crossing plane, keep their fields of view clear, and tune the distance threshold using serial diagnostics at the track.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
