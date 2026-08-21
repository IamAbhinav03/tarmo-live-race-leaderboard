#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_VL53L0X.h>
#include <esp_system.h>
#include <esp_timer.h>

#if __has_include("local_config.h")
#include "local_config.h"
#else
#include "config.example.h"
#warning "Using placeholder config.example.h; create include/local_config.h before deployment"
#endif

#ifndef USE_STATIC_IP
#define USE_STATIC_IP 0
#endif

#ifndef SENSOR_LONG_RANGE_MODE
#define SENSOR_LONG_RANGE_MODE 0
#endif

namespace {

constexpr size_t kEventQueueSize = 16;
constexpr size_t kJsonSize = 512;

Adafruit_VL53L0X sensorA;
Adafruit_VL53L0X sensorB;

enum class RangingSensor : uint8_t { A, B };

struct SensorState {
  uint16_t distanceMm = 8190;
  uint8_t rangeStatus = 0;
  float signalRateMcps = 0.0f;
  float ambientRateMcps = 0.0f;
  uint8_t nearSamples = 0;
  uint8_t clearSamples = 0;
  bool near = false;
  bool detectionSeen = false;
  uint32_t detectedAtMs = 0;
};

struct PendingEvent {
  bool occupied = false;
  char json[kJsonSize] = {};
  uint32_t lastAttemptMs = 0;
};

SensorState stateA;
SensorState stateB;
PendingEvent eventQueue[kEventQueueSize];
uint8_t queueHead = 0;
uint8_t queueTail = 0;
uint8_t queueCount = 0;

uint32_t bootId = 0;
uint32_t eventSequence = 0;
uint32_t lastWifiAttemptMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t crossingLockedAtMs = 0;
bool crossingLocked = false;
bool wifiConnectionReported = false;
bool rangeInProgress = false;
RangingSensor rangingSensor = RangingSensor::A;
int lastWifiDeliveryStatus = 0;
float detectionMinSignalRateMcps = DETECTION_MIN_SIGNAL_RATE_MCPS;

bool elapsedAtLeast(uint32_t now, uint32_t then, uint32_t interval) {
  return static_cast<uint32_t>(now - then) >= interval;
}

void emitFirmwareLog(const char *level, const char *code, const char *message,
                     const char *sensor = nullptr, uint16_t distanceMm = 0,
                     uint8_t rangeStatus = 0, float signalRateMcps = 0.0f) {
  ++eventSequence;
  char eventId[80];
  snprintf(eventId, sizeof(eventId), "%s-%08lx-%lu", DEVICE_ID,
           static_cast<unsigned long>(bootId), static_cast<unsigned long>(eventSequence));

  char json[kJsonSize];
  if (sensor != nullptr) {
    snprintf(
        json, sizeof(json),
        "{\"event_id\":\"%s\",\"device_id\":\"%s\",\"boot_id\":\"%08lx\","
        "\"sequence\":%lu,\"type\":\"log\",\"device_time_us\":%lld,"
        "\"level\":\"%s\",\"code\":\"%s\",\"message\":\"%s\","
        "\"sensor\":\"%s\",\"distance_mm\":%u,\"range_status\":%u,"
        "\"signal_rate_mcps\":%.4f}",
        eventId, DEVICE_ID, static_cast<unsigned long>(bootId),
        static_cast<unsigned long>(eventSequence), static_cast<long long>(esp_timer_get_time()),
        level, code, message, sensor, distanceMm, rangeStatus, signalRateMcps);
  } else {
    snprintf(
        json, sizeof(json),
        "{\"event_id\":\"%s\",\"device_id\":\"%s\",\"boot_id\":\"%08lx\","
        "\"sequence\":%lu,\"type\":\"log\",\"device_time_us\":%lld,"
        "\"level\":\"%s\",\"code\":\"%s\",\"message\":\"%s\"}",
        eventId, DEVICE_ID, static_cast<unsigned long>(bootId),
        static_cast<unsigned long>(eventSequence), static_cast<long long>(esp_timer_get_time()),
        level, code, message);
  }
  // Fine-grained telemetry stays on USB so it cannot delay sensor sampling or
  // displace timing events from the Wi-Fi retry queue.
  Serial.println(json);
}

bool isValidNearReading(uint16_t distanceMm, uint8_t rangeStatus,
                        float signalRateMcps) {
  // The ST/Adafruit API defines only status 0 as a valid range. In particular,
  // status 2 is a signal failure and must never become a detection candidate.
  return rangeStatus == 0 && distanceMm >= DETECTION_MIN_MM &&
         distanceMm <= DETECTION_MAX_MM &&
         signalRateMcps >= detectionMinSignalRateMcps;
}

void processSerialCommands() {
  static char command[96] = {};
  static size_t commandLength = 0;

  while (Serial.available() > 0) {
    const char next = static_cast<char>(Serial.read());
    if (next == '\r') {
      continue;
    }
    if (next != '\n') {
      if (commandLength + 1 < sizeof(command)) {
        command[commandLength++] = next;
      } else {
        commandLength = 0;
        emitFirmwareLog("warning", "detection_config_rejected",
                        "Serial configuration command was too long");
      }
      continue;
    }

    command[commandLength] = '\0';
    float requestedMcps = 0.0f;
    char trailing = '\0';
    const int fields = sscanf(command, "SET_MIN_SIGNAL_MCPS %f %c",
                              &requestedMcps, &trailing);
    if (fields == 1 && isfinite(requestedMcps) && requestedMcps >= 0.0f &&
        requestedMcps <= 20.0f) {
      detectionMinSignalRateMcps = requestedMcps;
      emitFirmwareLog("info", "detection_config_updated",
                      "Minimum target signal rate updated over USB");
    } else if (commandLength > 0) {
      emitFirmwareLog("warning", "detection_config_rejected",
                      "Invalid minimum target signal rate command");
    }
    commandLength = 0;
  }
}

void updateSensorState(SensorState &state, uint16_t distanceMm, uint8_t rangeStatus, uint32_t nowMs) {
  state.distanceMm = distanceMm;
  state.rangeStatus = rangeStatus;
  const bool sampleNear =
      isValidNearReading(distanceMm, rangeStatus, state.signalRateMcps);

  if (sampleNear) {
    state.clearSamples = 0;
    if (state.nearSamples < REQUIRED_NEAR_SAMPLES) {
      ++state.nearSamples;
    }
    if (!state.near && state.nearSamples >= REQUIRED_NEAR_SAMPLES) {
      state.near = true;
      state.detectionSeen = true;
      state.detectedAtMs = nowMs;
    }
  } else {
    state.nearSamples = 0;
    if (state.clearSamples < REQUIRED_CLEAR_SAMPLES) {
      ++state.clearSamples;
    }
    if (state.clearSamples >= REQUIRED_CLEAR_SAMPLES) {
      state.near = false;
    }
  }
}

void enqueueForWifi(const char *json) {
  if (queueCount == kEventQueueSize) {
    // Preserve the newest timing data if Wi-Fi has been down for a long time.
    eventQueue[queueHead].occupied = false;
    queueHead = (queueHead + 1) % kEventQueueSize;
    --queueCount;
    emitFirmwareLog("error", "wifi_queue_overflow",
                    "Oldest pending crossing dropped from Wi-Fi retry queue");
  }

  PendingEvent &slot = eventQueue[queueTail];
  slot.occupied = true;
  strlcpy(slot.json, json, sizeof(slot.json));
  slot.lastAttemptMs = 0;
  queueTail = (queueTail + 1) % kEventQueueSize;
  ++queueCount;
}

void emitCrossingEvent(int64_t deviceTimeUs) {
  ++eventSequence;
  char eventId[80];
  snprintf(eventId, sizeof(eventId), "%s-%08lx-%lu", DEVICE_ID,
           static_cast<unsigned long>(bootId), static_cast<unsigned long>(eventSequence));

  char json[kJsonSize];
  snprintf(
      json, sizeof(json),
      "{\"event_id\":\"%s\",\"device_id\":\"%s\",\"boot_id\":\"%08lx\","
      "\"sequence\":%lu,\"type\":\"crossing\",\"device_time_us\":%lld,"
      "\"sensor_a_mm\":%u,\"sensor_b_mm\":%u}",
      eventId, DEVICE_ID, static_cast<unsigned long>(bootId),
      static_cast<unsigned long>(eventSequence), static_cast<long long>(deviceTimeUs),
      stateA.distanceMm, stateB.distanceMm);

  // USB delivery is immediate; Wi-Fi uses the retry queue with the same ID.
  Serial.println(json);
  enqueueForWifi(json);
}

void updateCrossingDetector(uint32_t nowMs) {
  if (crossingLocked) {
    const bool bothClear = !stateA.near && !stateB.near;
    if (bothClear && elapsedAtLeast(nowMs, crossingLockedAtMs, CROSSING_LOCKOUT_MS)) {
      crossingLocked = false;
      stateA.detectionSeen = false;
      stateB.detectionSeen = false;
      emitFirmwareLog("info", "gate_rearmed", "Both sensors clear; gate re-armed");
    }
    return;
  }

  if (stateA.detectionSeen && stateB.detectionSeen) {
    const uint32_t difference = stateA.detectedAtMs > stateB.detectedAtMs
                                    ? stateA.detectedAtMs - stateB.detectedAtMs
                                    : stateB.detectedAtMs - stateA.detectedAtMs;
    if (difference <= SENSOR_COINCIDENCE_MS) {
      emitCrossingEvent(esp_timer_get_time());
      crossingLocked = true;
      crossingLockedAtMs = nowMs;
      Serial.printf("DIAG confirmed crossing A=%umm B=%umm delta=%lums\n",
                    stateA.distanceMm, stateB.distanceMm, static_cast<unsigned long>(difference));
      return;
    }
  }

  // An unpaired detection is discarded after the window, but the sensor must
  // physically clear before it can create a fresh candidate.
  if (stateA.detectionSeen && !stateB.detectionSeen &&
      elapsedAtLeast(nowMs, stateA.detectedAtMs, SENSOR_COINCIDENCE_MS)) {
    stateA.detectionSeen = false;
    emitFirmwareLog("warning", "sensor_single_rejected",
                    "Sensor A activation expired without Sensor B", "A",
                    stateA.distanceMm, stateA.rangeStatus, stateA.signalRateMcps);
  }
  if (stateB.detectionSeen && !stateA.detectionSeen &&
      elapsedAtLeast(nowMs, stateB.detectedAtMs, SENSOR_COINCIDENCE_MS)) {
    stateB.detectionSeen = false;
    emitFirmwareLog("warning", "sensor_single_rejected",
                    "Sensor B activation expired without Sensor A", "B",
                    stateB.distanceMm, stateB.rangeStatus, stateB.signalRateMcps);
  }
}

bool initializeSensors() {
  pinMode(SENSOR_A_XSHUT_PIN, OUTPUT);
  pinMode(SENSOR_B_XSHUT_PIN, OUTPUT);
  digitalWrite(SENSOR_A_XSHUT_PIN, LOW);
  digitalWrite(SENSOR_B_XSHUT_PIN, LOW);
  delay(20);

  digitalWrite(SENSOR_A_XSHUT_PIN, HIGH);
  delay(20);
  if (!sensorA.begin(SENSOR_A_I2C_ADDRESS, false, &Wire)) {
    emitFirmwareLog("error", "sensor_init_failed", "Sensor A failed to initialize", "A");
    return false;
  }

  digitalWrite(SENSOR_B_XSHUT_PIN, HIGH);
  delay(20);
  if (!sensorB.begin(SENSOR_B_I2C_ADDRESS, false, &Wire)) {
    emitFirmwareLog("error", "sensor_init_failed", "Sensor B failed to initialize", "B");
    return false;
  }

#if SENSOR_LONG_RANGE_MODE
  const bool sensorAConfigured =
      sensorA.configSensor(Adafruit_VL53L0X::VL53L0X_SENSE_LONG_RANGE);
  const bool sensorBConfigured =
      sensorB.configSensor(Adafruit_VL53L0X::VL53L0X_SENSE_LONG_RANGE);
#else
  const bool sensorAConfigured =
      sensorA.configSensor(Adafruit_VL53L0X::VL53L0X_SENSE_HIGH_SPEED);
  const bool sensorBConfigured =
      sensorB.configSensor(Adafruit_VL53L0X::VL53L0X_SENSE_HIGH_SPEED);
#endif
  const FixPoint1616_t signalRateLimit = static_cast<FixPoint1616_t>(
      SENSOR_SIGNAL_RATE_LIMIT_MCPS * 65536.0f);
  const bool sensorASettingsApplied =
      sensorAConfigured &&
      sensorA.setLimitCheckEnable(VL53L0X_CHECKENABLE_RANGE_IGNORE_THRESHOLD, 0) &&
      sensorA.setLimitCheckValue(VL53L0X_CHECKENABLE_SIGNAL_RATE_FINAL_RANGE,
                                 signalRateLimit) &&
      sensorA.setMeasurementTimingBudgetMicroSeconds(SENSOR_TIMING_BUDGET_US);
  const bool sensorBSettingsApplied =
      sensorBConfigured &&
      sensorB.setLimitCheckEnable(VL53L0X_CHECKENABLE_RANGE_IGNORE_THRESHOLD, 0) &&
      sensorB.setLimitCheckValue(VL53L0X_CHECKENABLE_SIGNAL_RATE_FINAL_RANGE,
                                 signalRateLimit) &&
      sensorB.setMeasurementTimingBudgetMicroSeconds(SENSOR_TIMING_BUDGET_US);
  if (!sensorASettingsApplied || !sensorBSettingsApplied) {
    emitFirmwareLog("error", "sensor_config_failed",
                    "VL53L0X ranging configuration failed");
    return false;
  }
  rangingSensor = RangingSensor::A;
  rangeInProgress = sensorA.startRange();
  if (!rangeInProgress) {
    emitFirmwareLog("error", "sensor_range_start_failed",
                    "Sensor A failed to start staggered ranging", "A");
    return false;
  }
  emitFirmwareLog("info", "sensors_ready", "Both VL53L0X sensors initialized");
  return true;
}

void maintainWifi(uint32_t nowMs) {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiConnectionReported) {
      wifiConnectionReported = true;
      emitFirmwareLog("info", "wifi_connected", "Wi-Fi connected using configured network mode");
    }
    return;
  }
  if (wifiConnectionReported) {
    wifiConnectionReported = false;
    emitFirmwareLog("warning", "wifi_disconnected", "Wi-Fi connection lost");
  }
  if (!elapsedAtLeast(nowMs, lastWifiAttemptMs, WIFI_RETRY_INTERVAL_MS)) {
    return;
  }
  lastWifiAttemptMs = nowMs;

  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_STA);
#if USE_STATIC_IP
  IPAddress localIp(ESP_STATIC_IP);
  IPAddress gateway(ESP_GATEWAY_IP);
  IPAddress subnet(ESP_SUBNET_MASK);
  IPAddress dns(ESP_DNS_IP);
  if (!WiFi.config(localIp, gateway, subnet, dns)) {
    emitFirmwareLog("error", "static_ip_failed", "Static IP configuration failed");
  }
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  emitFirmwareLog("info", "wifi_connecting",
                  USE_STATIC_IP ? "Wi-Fi static-IP connection attempt started"
                                : "Wi-Fi DHCP connection attempt started");
}

void deliverPendingWifi(uint32_t nowMs) {
  if (queueCount == 0 || WiFi.status() != WL_CONNECTED) {
    return;
  }
  PendingEvent &event = eventQueue[queueHead];
  if (event.lastAttemptMs != 0 &&
      !elapsedAtLeast(nowMs, event.lastAttemptMs, EVENT_RETRY_INTERVAL_MS)) {
    return;
  }
  event.lastAttemptMs = nowMs;

  WiFiClient client;
  HTTPClient http;
  char url[160];
  snprintf(url, sizeof(url), "http://%s:%u/api/device/events", SERVER_HOST, SERVER_PORT);
  // Bound Wi-Fi work below the crossing lockout so a failed network cannot
  // prevent the sensors from re-arming for the next lap crossing.
  http.setConnectTimeout(150);
  http.setTimeout(200);
  if (!http.begin(client, url)) {
    return;
  }
  http.addHeader("Content-Type", "application/json");
  const int status = http.POST(String(event.json));
  http.end();
  if (status >= 200 && status < 300) {
    if (lastWifiDeliveryStatus < 200 || lastWifiDeliveryStatus >= 300) {
      emitFirmwareLog("info", "wifi_delivery_restored",
                      "Wi-Fi crossing delivery accepted by server");
    }
    lastWifiDeliveryStatus = status;
    event.occupied = false;
    queueHead = (queueHead + 1) % kEventQueueSize;
    --queueCount;
  } else {
    if (lastWifiDeliveryStatus != status) {
      emitFirmwareLog("warning", "wifi_delivery_failed",
                      "Wi-Fi crossing delivery failed; event remains queued");
    }
    lastWifiDeliveryStatus = status;
  }
}

void processCompletedReading(Adafruit_VL53L0X &sensor, SensorState &state,
                             const char *sensorName, const char *activatedMessage,
                             const char *clearedMessage) {
  VL53L0X_RangingMeasurementData_t measurement = {};
  const VL53L0X_Error readStatus = sensor.getRangingMeasurement(&measurement);
  sensor.clearInterruptMask();
  if (readStatus != VL53L0X_ERROR_NONE) {
    emitFirmwareLog("error", "sensor_read_failed",
                    "VL53L0X measurement read failed", sensorName);
    return;
  }
  const uint16_t distanceMm = measurement.RangeMilliMeter;
  const uint8_t rangeStatus = measurement.RangeStatus;
  state.signalRateMcps =
      static_cast<float>(measurement.SignalRateRtnMegaCps) / 65536.0f;
  state.ambientRateMcps =
      static_cast<float>(measurement.AmbientRateRtnMegaCps) / 65536.0f;
  const bool wasNear = state.near;
  updateSensorState(state, distanceMm, rangeStatus, millis());
  if (!wasNear && state.near) {
    emitFirmwareLog("info", "sensor_activated", activatedMessage, sensorName,
                    distanceMm, rangeStatus, state.signalRateMcps);
  } else if (wasNear && !state.near) {
    emitFirmwareLog("info", "sensor_cleared", clearedMessage, sensorName,
                    distanceMm, rangeStatus, state.signalRateMcps);
  }
}

void readSensors() {
  // Alternate non-blocking single shots. Only one laser emits at a time, which
  // prevents the side-by-side VL53L0X modules from optically interfering.
  Adafruit_VL53L0X &activeSensor = rangingSensor == RangingSensor::A ? sensorA : sensorB;
  if (rangeInProgress && !activeSensor.isRangeComplete()) {
    return;
  }
  if (rangeInProgress) {
    if (rangingSensor == RangingSensor::A) {
      processCompletedReading(sensorA, stateA, "A", "Sensor A entered detection range",
                              "Sensor A cleared");
      rangingSensor = RangingSensor::B;
    } else {
      processCompletedReading(sensorB, stateB, "B", "Sensor B entered detection range",
                              "Sensor B cleared");
      rangingSensor = RangingSensor::A;
    }
    rangeInProgress = false;
  }

  Adafruit_VL53L0X &nextSensor = rangingSensor == RangingSensor::A ? sensorA : sensorB;
  rangeInProgress = nextSensor.startRange();
}

void emitSensorStatus() {
  Serial.printf(
      "{\"type\":\"sensor_status\",\"device_id\":\"%s\",\"boot_id\":\"%08lx\","
      "\"device_time_us\":%lld,\"a_mm\":%u,\"a_status\":%u,\"a_near\":%u,"
      "\"a_candidate\":%u,\"a_signal_mcps\":%.4f,\"a_ambient_mcps\":%.4f,"
      "\"b_mm\":%u,\"b_status\":%u,\"b_near\":%u,\"b_candidate\":%u,"
      "\"b_signal_mcps\":%.4f,\"b_ambient_mcps\":%.4f,\"gate_locked\":%u,"
      "\"min_signal_mcps\":%.4f}\n",
      DEVICE_ID, static_cast<unsigned long>(bootId),
      static_cast<long long>(esp_timer_get_time()), stateA.distanceMm,
      stateA.rangeStatus, stateA.near ? 1 : 0, stateA.detectionSeen ? 1 : 0,
      stateA.signalRateMcps, stateA.ambientRateMcps,
      stateB.distanceMm, stateB.rangeStatus, stateB.near ? 1 : 0,
      stateB.detectionSeen ? 1 : 0, stateB.signalRateMcps,
      stateB.ambientRateMcps, crossingLocked ? 1 : 0,
      detectionMinSignalRateMcps);
}

}  // namespace

void setup() {
  // Live diagnostic frames exceed the ESP32-C3 HW-CDC default 256-byte TX
  // ring. A larger ring prevents adjacent JSON frames from being truncated or
  // joined when the host reads USB in small chunks.
  Serial.setTxBufferSize(1024);
  Serial.begin(115200);
  delay(1000);
  bootId = esp_random();
  emitFirmwareLog("info", "device_boot", "Tarmo timing gate booted");

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(I2C_CLOCK_HZ);
  if (!initializeSensors()) {
    emitFirmwareLog("error", "sensor_initialization_halted",
                    "Sensor initialization halted; check power, XSHUT, and I2C wiring");
    while (true) {
      delay(1000);
    }
  }

  lastWifiAttemptMs = millis() - WIFI_RETRY_INTERVAL_MS;
  maintainWifi(millis());
}

void loop() {
  processSerialCommands();
  readSensors();
  // Capture time after polling: a completed asynchronous measurement may have
  // set detectedAtMs during readSensors(), and nowMs must not predate it.
  const uint32_t nowMs = millis();
  updateCrossingDetector(nowMs);
  if (elapsedAtLeast(nowMs, lastTelemetryMs, SENSOR_TELEMETRY_INTERVAL_MS)) {
    lastTelemetryMs = nowMs;
    emitSensorStatus();
  }
  maintainWifi(nowMs);
  deliverPendingWifi(nowMs);
  delay(1);
}
