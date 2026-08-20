#pragma once

// Copy this file to local_config.h and set values for the track installation.

#define WIFI_SSID "YOUR_WIFI_NAME"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// Track-side computer. Use its LAN IP, not localhost.
#define SERVER_HOST "192.168.1.10"
#define SERVER_PORT 8080

// Use DHCP while testing on ordinary Wi-Fi. Set this to 1 for the final
// track installation, then configure all four network addresses below.
#define USE_STATIC_IP 0

// Static address for the final installation. Ignored when USE_STATIC_IP is 0.
#define ESP_STATIC_IP 192, 168, 1, 50
#define ESP_GATEWAY_IP 192, 168, 1, 1
#define ESP_SUBNET_MASK 255, 255, 255, 0
#define ESP_DNS_IP 192, 168, 1, 1

// Seeed Studio XIAO ESP32-C3 wiring:
// D4/GPIO6 = SDA, D5/GPIO7 = SCL, D2/GPIO4 and D3/GPIO5 = XSHUT.
#define I2C_SDA_PIN 6
#define I2C_SCL_PIN 7
#define SENSOR_A_XSHUT_PIN 4
#define SENSOR_B_XSHUT_PIN 5

#define SENSOR_A_I2C_ADDRESS 0x30
#define SENSOR_B_I2C_ADDRESS 0x31

// Tune these values at the installed finish line.
#define DETECTION_MIN_MM 25
#define DETECTION_MAX_MM 650
#define REQUIRED_NEAR_SAMPLES 2
#define REQUIRED_CLEAR_SAMPLES 3
#define SENSOR_COINCIDENCE_MS 120
#define CROSSING_LOCKOUT_MS 800
#define SENSOR_TIMING_BUDGET_US 20000

#define DEVICE_ID "tarmo-gate-01"
#define WIFI_RETRY_INTERVAL_MS 5000
#define EVENT_RETRY_INTERVAL_MS 750
