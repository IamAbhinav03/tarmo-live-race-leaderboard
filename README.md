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

| XIAO ESP32-C3 | Sensor A | Sensor B |
|---|---|---|
| 3V3 | VIN/VCC | VIN/VCC |
| GND | GND | GND |
| D4 / GPIO6 | SDA | SDA |
| D5 / GPIO7 | SCL | SCL |
| D2 / GPIO4 | XSHUT | — |
| D3 / GPIO5 | — | XSHUT |

The firmware targets the Seeed Studio XIAO ESP32-C3 using PlatformIO's `seeed_xiao_esp32c3` board definition. Use 3.3 V logic and follow the voltage specification of your VL53L0X breakout board.

## Firmware setup

1. Install [PlatformIO](https://platformio.org/).
2. Copy `firmware/include/config.example.h` to `firmware/include/local_config.h`.
3. Set the Wi-Fi credentials, track-side server address, static IP details, and GPIO pins in `local_config.h`.
4. From `firmware/`, run `pio run --target upload`, then `pio device monitor`.

`local_config.h` is intentionally ignored by Git so credentials and site-specific network values are not published.

For breadboard testing on ordinary Wi-Fi, set `USE_STATIC_IP` to `0`. The ESP32 will obtain its own address with DHCP; `SERVER_HOST` must still be the current LAN address of the computer running the race server. For the final track installation, reserve addresses on the router or set `USE_STATIC_IP` to `1` with confirmed network values.

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

### Breadboard test sequence

1. **Power-on check:** Start the serial monitor. Confirm that both sensors initialize at `0x30` and `0x31`. A missing sensor must stop the gate rather than silently operate with one sensor.
2. **Noise rejection:** Cover only Sensor A, then only Sensor B. Neither action should produce a JSON crossing event.
3. **Coincidence check:** Move one flat target through both sensor fields together. Exactly one crossing event should appear. Keep the target present and verify that no repeated event appears.
4. **Re-arm check:** Remove the target completely, wait for the lockout, then pass it through both fields again. A second event should appear.
5. **USB race test:** Start the server, register a driver, and arm the race. The first crossing must change the server from `armed` to `running`; the second must finish the lap. The server—not the ESP32—subtracts the two `device_time_us` timestamps.
6. **Dual-path test:** Leave USB and Wi-Fi connected. Each crossing is delivered twice with the same event ID, but the server must advance the race only once.
7. **Failure tests:** Repeat a race once with Wi-Fi disconnected to verify USB-only timing, and once with USB disconnected after starting the server to verify Wi-Fi-only timing.
8. **Placement calibration:** At realistic car speed, tune `DETECTION_MAX_MM`, required stable samples, coincidence window, and lockout. Test bright light, dark bodywork, angled approaches, and a stationary object near only one sensor.
