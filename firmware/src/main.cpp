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

namespace {

constexpr size_t kEventQueueSize = 16;
constexpr size_t kJsonSize = 384;

Adafruit_VL53L0X sensorA;
Adafruit_VL53L0X sensorB;

struct SensorState {
  uint16_t distanceMm = 8190;
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
uint32_t crossingLockedAtMs = 0;
bool crossingLocked = false;

bool elapsedAtLeast(uint32_t now, uint32_t then, uint32_t interval) {
  return static_cast<uint32_t>(now - then) >= interval;
}

bool isValidNearReading(uint16_t distanceMm, uint8_t rangeStatus) {
  return rangeStatus != 4 && distanceMm >= DETECTION_MIN_MM && distanceMm <= DETECTION_MAX_MM;
}

void updateSensorState(SensorState &state, uint16_t distanceMm, uint8_t rangeStatus, uint32_t nowMs) {
  state.distanceMm = distanceMm;
  const bool sampleNear = isValidNearReading(distanceMm, rangeStatus);

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

  // USB delivery is immediate. Diagnostic output never begins with "{" so the
  // bridge can distinguish it from machine-readable events.
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
      Serial.println("DIAG crossing detector re-armed");
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
    Serial.println("DIAG rejected sensor A-only detection");
  }
  if (stateB.detectionSeen && !stateA.detectionSeen &&
      elapsedAtLeast(nowMs, stateB.detectedAtMs, SENSOR_COINCIDENCE_MS)) {
    stateB.detectionSeen = false;
    Serial.println("DIAG rejected sensor B-only detection");
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
    Serial.println("ERROR sensor A failed to initialize");
    return false;
  }

  digitalWrite(SENSOR_B_XSHUT_PIN, HIGH);
  delay(20);
  if (!sensorB.begin(SENSOR_B_I2C_ADDRESS, false, &Wire)) {
    Serial.println("ERROR sensor B failed to initialize");
    return false;
  }

  sensorA.configSensor(Adafruit_VL53L0X::VL53L0X_SENSE_HIGH_SPEED);
  sensorB.configSensor(Adafruit_VL53L0X::VL53L0X_SENSE_HIGH_SPEED);
  sensorA.setMeasurementTimingBudgetMicroSeconds(SENSOR_TIMING_BUDGET_US);
  sensorB.setMeasurementTimingBudgetMicroSeconds(SENSOR_TIMING_BUDGET_US);
  Serial.println("DIAG both VL53L0X sensors ready");
  return true;
}

void maintainWifi(uint32_t nowMs) {
  if (WiFi.status() == WL_CONNECTED) {
    return;
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
    Serial.println("ERROR static IP configuration failed");
  }
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("DIAG Wi-Fi connection attempt started mode=%s\n",
                USE_STATIC_IP ? "static" : "dhcp");
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
    event.occupied = false;
    queueHead = (queueHead + 1) % kEventQueueSize;
    --queueCount;
  }
}

void readSensors(uint32_t nowMs) {
  VL53L0X_RangingMeasurementData_t measurementA;
  VL53L0X_RangingMeasurementData_t measurementB;
  sensorA.rangingTest(&measurementA, false);
  sensorB.rangingTest(&measurementB, false);
  updateSensorState(stateA, measurementA.RangeMilliMeter, measurementA.RangeStatus, nowMs);
  updateSensorState(stateB, measurementB.RangeMilliMeter, measurementB.RangeStatus, nowMs);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  bootId = esp_random();
  Serial.printf("DIAG Tarmo gate boot=%08lx device=%s\n",
                static_cast<unsigned long>(bootId), DEVICE_ID);

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);
  if (!initializeSensors()) {
    Serial.println("ERROR sensor initialization halted; check power, XSHUT pins, and I2C wiring");
    while (true) {
      delay(1000);
    }
  }

  lastWifiAttemptMs = millis() - WIFI_RETRY_INTERVAL_MS;
  maintainWifi(millis());
}

void loop() {
  const uint32_t nowMs = millis();
  readSensors(nowMs);
  updateCrossingDetector(nowMs);
  maintainWifi(nowMs);
  deliverPendingWifi(nowMs);
  delay(1);
}
