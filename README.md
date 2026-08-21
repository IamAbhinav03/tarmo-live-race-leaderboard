# Tarmo Live Competition System

A local competition system with two live leaderboards: the sensor-timed **Tarmo Live** race and the manually measured **Cannon Clash**. Two VL53L0X sensors must agree on a race crossing before the ESP32-C3 emits an event. Every event is sent over Wi-Fi and USB serial; the track-side server deduplicates the two copies, stores all results in SQLite, and updates the operator console and public display live.

## How a race works

1. An operator opens `/operator`, enters the driver's name, and arms a race.
2. The first confirmed two-sensor crossing starts the timer.
3. The second confirmed crossing finishes the single lap.
4. The firmware's microsecond clock supplies the official elapsed time, so network or USB latency does not affect the result.
5. The result is persisted and appears immediately on `/leaderboard`, sorted fastest first.

## Sensing glossary

| Term | Meaning in this project |
|---|---|
| **Arm** | The operator registers a driver and tells the server that the next confirmed crossing should start that driver's lap. |
| **Armed** | A race is waiting for its first confirmed crossing. The sensors continue running whether or not a race is armed. |
| **Raw sample** | One distance and range-status result returned by a VL53L0X sensor. Raw samples are not logged individually. |
| **Near reading** | A raw sample whose distance is inside `DETECTION_MIN_MM` through `DETECTION_MAX_MM` and whose current range status is accepted by the firmware. |
| **Stable** | A condition that has remained consistent for the configured number of consecutive samples, rather than appearing in only one potentially noisy sample. |
| **Stable-near** | A sensor has produced `REQUIRED_NEAR_SAMPLES` consecutive valid near readings. The race configuration uses one strict status-0 sample from each of the two sensors, so every crossing still requires two independent confirmations. |
| **Activated** | The transition from not-near to stable-near. It creates a crossing candidate and emits `sensor_activated`. |
| **Crossing candidate** | A remembered sensor activation waiting for the other sensor to activate within `SENSOR_COINCIDENCE_MS`. |
| **Coincidence window** | The maximum permitted difference between the two activation times. It is currently 300 ms. |
| **Confirmed crossing** | Both sensors became stable-near inside the coincidence window. One timestamped crossing event is then emitted over USB and queued for Wi-Fi. |
| **Single-sensor rejection** | One sensor activated but the other did not join it inside the coincidence window. It emits `sensor_single_rejected` and creates no crossing. |
| **Lockout** | The period after a confirmed crossing during which another crossing cannot be emitted. It prevents one car pass or reflection from being counted repeatedly. |
| **Clear reading** | A raw sample that does not qualify as near. |
| **Stable-clear** | A sensor has produced `REQUIRED_CLEAR_SAMPLES` consecutive clear readings. With the current configuration, this means three readings. |
| **Cleared** | The transition from stable-near to stable-clear. It emits `sensor_cleared`. |
| **Re-arm / re-armed** | The timing gate becomes ready to detect another crossing after both sensors are stable-clear and the crossing lockout has elapsed. It emits `gate_rearmed`. This is separate from arming a driver's race. |
| **Device timestamp** | The ESP32's microsecond timestamp attached to a confirmed crossing. The server subtracts the first timestamp from the second to calculate the lap time. |
| **Duplicate event** | The same crossing arrives through USB and Wi-Fi with the same event ID. The server accepts it once and logs the other delivery as `event_duplicate`. |

## How Cannon Clash works

1. In `/operator`, select **Cannon Clash**.
2. Enter the participant, measured distance, and measurement unit.
3. Record as many attempts as needed. Every attempt is retained in SQLite.
4. The public leaderboard shows each participant once, ranked by their longest attempt.

Distances can be entered in metres, centimetres, feet, inches, or millimetres. The server normalizes them to millimetres before comparison. The public display starts in split mode; use its controls to show only Tarmo Live, only Cannon Clash, or enter fullscreen. The selected layout is also addressable with `?view=both`, `?view=race`, or `?view=cannon`.

## Project layout

```text
firmware/           ESP32-C3 PlatformIO/Arduino firmware
server/             Zero-dependency Python race server and USB bridge
server/static/      Operator and leaderboard web screens
tests/              Server and timing tests
```

## Setup guide

The track-side server supports Python 3.10 or newer on macOS or Linux. It has no third-party Python dependencies, but a `requirements.txt` is included so the usual virtual-environment workflow remains reproducible.

### 1. Download and prepare the server

```bash
git clone https://github.com/IamAbhinav03/tarmo-live-race-leaderboard.git
cd tarmo-live-race-leaderboard
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp server/config.example.json server/config.json
```

The default configuration listens on port `8080`, stores persistent data in `server/data/races.db`, and automatically discovers common USB serial devices. To run without an ESP32 connected, change `serial_port` in `server/config.json` from `"auto"` to `null`.

### 2. Start the leaderboard server

Run this command from the repository root:

```bash
python3 server/app.py
```

Keep that terminal open. Then open:

- Operator console: [http://localhost:8080/operator](http://localhost:8080/operator)
- Competition display: [http://localhost:8080/leaderboard](http://localhost:8080/leaderboard)

Other computers on the same network can use `http://TRACK_COMPUTER_IP:8080/operator` and `http://TRACK_COMPUTER_IP:8080/leaderboard`. Allow incoming TCP port `8080` in the computer's firewall if necessary. Stop the server with `Ctrl+C`.

### 3. Configure and upload the ESP32 firmware

1. Install [PlatformIO](https://platformio.org/), or install its extension in VS Code.
2. Copy `firmware/include/config.example.h` to `firmware/include/local_config.h`.
3. In `local_config.h`, set the Wi-Fi credentials and set `SERVER_HOST` to the track-side computer's LAN IP address. Use `USE_STATIC_IP 0` while testing on normal Wi-Fi.
4. Connect the Seeed Studio XIAO ESP32-C3 over USB.
5. Upload and monitor the firmware:

```bash
cd firmware
pio run --target upload
pio device monitor
```

Both the computer and ESP32 must be on the same network for Wi-Fi delivery. USB delivery works whenever the ESP32 is connected to the track-side computer, providing the redundant communication path.

### 4. Verify the installation

1. Open the operator console and confirm that the server status is live.
2. Confirm the timing gate appears after the ESP32 sends its first USB or Wi-Fi event.
3. Select **Tarmo Live**, register a test driver, and make two valid crossings.
4. Select **Cannon Clash**, enter a test distance, and confirm it appears on the public display.
5. On the public display, verify the **Split**, **Tarmo Live**, **Cannon Clash**, and **Fullscreen** controls.

To run the automated checks:

```bash
python3 -m unittest discover -s tests -v
node tests/test_leaderboard.cjs
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

## Track-side server details

The server automatically looks for `/dev/cu.usbmodem*` and `/dev/cu.usbserial*` on macOS and `/dev/ttyACM*` or `/dev/ttyUSB*` on Linux. To select a device explicitly, set `serial_port` in `server/config.json`. Set it to `null` to disable USB ingestion.

The SQLite database is stored at `server/data/races.db` by default. Back up that file to preserve race history, cannon attempts, and audit logs.

## Persistent timing logs

The operator page includes a live, filterable audit log. Logs are appended to the `audit_logs` table in the same SQLite database as the races, so they survive browser refreshes and server restarts. Each row includes a UTC timestamp, severity, source, code, message, transport, event ID, race ID, and structured details.

The server records every transport attempt, not only the first copy of an event:

- `event_accepted`: the first USB or Wi-Fi copy was stored.
- `event_duplicate`: the other path delivered the same event ID and was safely deduplicated.
- `event_id_conflict`: an event ID was reused with different contents; the conflicting copy was rejected.
- `crossing_ignored_no_active_race`: a real crossing arrived while no driver was armed.
- `race_started`, `race_finished`, `race_failed_device_restart`: official timing decisions.
- `event_rejected`: malformed input or an impossible timestamp was rejected.
- `usb_connected`, `usb_disconnected`: serial bridge state.
- `sensor_activated`, `sensor_cleared`, `sensor_single_rejected`, `gate_rearmed`: firmware sensor decisions received over USB.
- `wifi_connected`, `wifi_disconnected`, `wifi_delivery_failed`, `wifi_delivery_restored`: firmware connectivity transitions received over USB.

Fine-grained sensor telemetry is intentionally USB-only. Sending every sensor transition over Wi-Fi would add blocking network work to the measurement loop and could displace official crossings from the retry queue. Official crossing events still use both USB and Wi-Fi with the same event ID.

### Tuning the minimum MCPS value

The operator page has a persistent **Minimum MCPS** control beside the live sensor readings. It filters otherwise valid status-0 readings whose returned target signal is weaker than the configured floor. The server saves the value in SQLite, sends it to the ESP32 immediately over USB, and reapplies it whenever the board reconnects.

1. Start at `0.00` so no status-0 reading is removed by the signal filter.
2. Make at least five full-speed passes with the real car.
3. Set the audit-log filter to **Sensors** and record `signal_rate_mcps` from every genuine `sensor_activated` entry for both A and B.
4. Start with 70–80% of the weakest genuine activation. For example, if the weakest car reading is `0.60 MCPS`, try `0.45 MCPS`.
5. Leave the track idle for several minutes. If false activations remain, increase the value by only `0.05 MCPS`, then repeat the full-speed car test.
6. If false readings overlap the genuine car's signal rates, no MCPS threshold can separate them reliably. Improve physical shading, alignment, or the background target instead of raising the floor until the car is missed.

The panel distinguishes the saved **Configured** value from the value reported by the firmware as **ESP applied**. `USB live` confirms that changes can currently reach the board.

Recent logs are available as JSON at `GET /api/logs?limit=250`. Up to 1,000 rows can be requested at once, and older pages use `before_id`. Logs remain in SQLite across restarts. `audit_log_limit` defaults to 100,000 rows so a long-running noisy sensor cannot fill the computer's disk; increase it in `server/config.json` if longer retention is required.

## Safe edge-case emulator

The emulator runs on a separate port and database, with no serial device, so it cannot alter the real leaderboard:

```bash
python3 server/app.py --config server/config.emulator.example.json
python3 tools/emulate_edge_cases.py --server http://127.0.0.1:8081
```

Open `http://localhost:8081/operator` to inspect the generated logs. The emulator endpoint is disabled in the normal server configuration and rejects non-local clients even when enabled.

The automated scenario checks a crossing with no armed race, structured sensor telemetry, USB-first/Wi-Fi-second deduplication, Wi-Fi-first/USB-second deduplication, a normal timed lap, an ESP32 restart during a lap, a non-monotonic timestamp, and a malformed payload.

## Track validation matrix

| Test | How to emulate | Expected audit log and behavior |
|---|---|---|
| Sensor A only | Cover only A for more than 120 ms, then uncover it | `sensor_activated` A, `sensor_single_rejected` A, `sensor_cleared`; no crossing and no race transition |
| Sensor B only | Repeat with B | Same B sequence; no crossing |
| Sensors too far apart | Cover A, wait more than 120 ms, then cover B | Two single-sensor rejections; no crossing |
| Valid crossing | Move a flat target through both beams together | A and B activation, one `event_accepted`, race starts or finishes |
| Object held in beam | Keep the target present for several seconds | Only one crossing; no second crossing until both sensors clear and `gate_rearmed` appears |
| Reflection/bounce during lockout | Remove and immediately reinsert within 800 ms | No additional crossing before re-arm |
| No race armed | Perform a valid crossing from the grid-open state | `crossing_ignored_no_active_race`; leaderboard unchanged |
| Both communication paths | Keep USB and Wi-Fi connected and cross once | One `event_accepted` and one `event_duplicate` with the same event ID; race advances once |
| Wi-Fi unavailable | Stop the server or use an unreachable `SERVER_HOST`, keep USB connected, and cross | USB accepts immediately; `wifi_delivery_failed`; once restored, Wi-Fi copy becomes `event_duplicate` |
| USB unavailable | Unplug USB while powering the gate separately, keep Wi-Fi/server active, and cross | Wi-Fi `event_accepted`; `usb_disconnected`; sensor-level logs are unavailable during the unplugged interval |
| Server unavailable on both paths | Stop the server, cross, then restart without rebooting ESP32 | Wi-Fi event retries; USB serial data is only recoverable if a bridge was reading it. Queued Wi-Fi crossing arrives after restart |
| ESP32 restart before finish | Start a race, reset the ESP32, then cross again | `race_failed_device_restart`; race becomes timing error, never a false lap time |
| Duplicate/replayed event | Run the software emulator | `event_duplicate`; active race does not advance twice |
| Timestamp moves backward | Run the software emulator | HTTP 400 and `event_rejected`; active race remains running until cancelled |
| Malformed event | Run the software emulator | HTTP 400 and `event_rejected`; no race change |
| Sensor unplugged at boot | Power off, disconnect one module, then boot | `sensor_init_failed` and `sensor_initialization_halted`; gate fails closed |
| Persistence | Complete a test, stop and restart the server | Races and audit rows remain visible |

For the physical tests, start with no active race unless the row explicitly tests a race transition. Clear both beams between cases and wait for `gate_rearmed`. Pause the log view when inspecting a noisy sequence; pausing affects only the browser, not SQLite recording.

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
node tests/test_leaderboard.cjs
```

The leaderboard regression test verifies that sensor telemetry, duplicate deliveries, and unchanged state envelopes do not rebuild or reanimate either leaderboard. Only a genuine change to the corresponding race or cannon ranking may replace its rows.

### Breadboard test sequence

1. **Power-on check:** Start the serial monitor. Confirm that both sensors initialize at `0x30` and `0x31`. A missing sensor must stop the gate rather than silently operate with one sensor.
2. **Noise rejection:** Cover only Sensor A, then only Sensor B. Neither action should produce a JSON crossing event.
3. **Coincidence check:** Move one flat target through both sensor fields together. Exactly one crossing event should appear. Keep the target present and verify that no repeated event appears.
4. **Re-arm check:** Remove the target completely, wait for the lockout, then pass it through both fields again. A second event should appear.
5. **USB race test:** Start the server, register a driver, and arm the race. The first crossing must change the server from `armed` to `running`; the second must finish the lap. The server—not the ESP32—subtracts the two `device_time_us` timestamps.
6. **Dual-path test:** Leave USB and Wi-Fi connected. Each crossing is delivered twice with the same event ID, but the server must advance the race only once.
7. **Failure tests:** Repeat a race once with Wi-Fi disconnected to verify USB-only timing, and once with USB disconnected after starting the server to verify Wi-Fi-only timing.
8. **Placement calibration:** At realistic car speed, tune `DETECTION_MAX_MM`, required stable samples, coincidence window, and lockout. Test bright light, dark bodywork, angled approaches, and a stationary object near only one sensor.

Set `SENSOR_LONG_RANGE_MODE` to `1` with a 33,000 µs timing budget when experimentally testing beyond the high-speed profile's normal range. Long range trades sample speed and ambient-light tolerance for distance; validate it with the actual car and lighting before race use.
