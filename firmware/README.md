# Smart Watering Firmware

ESP-IDF firmware for `ESP32-C3`.

This firmware:

- reads weight from `HX711`
- stores tare, zero and scale calibration in `NVS`
- stores device type, device name and dry weight in `NVS`
- controls pump start via GPIO for `tank` devices
- exposes a small HTTP API
- stays online for 30 seconds after Wi-Fi connect
- stays awake past that window while watering is active
- then shuts down peripherals and enters `deep sleep`
- wakes from deep sleep after the configured `sleep_interval_min` interval, default `15`

## Device Types

- `plant`: default device type, used for weight/metrics.
- `tank`: device type allowed to run pump watering commands.

`dry_weight_g` is stored in `NVS` and returned by the API, but it does not participate in weight calculations yet.

## Weight Model

- `gross_weight_g`: total weight on the scale
- `tare_weight_g`: configured tare
- `useful_weight_g`: `gross_weight_g - tare_weight_g`
- `water_used_g`: tracked amount of water used by the current or last watering operation

`useful_weight_g` is the value used by watering logic.

## Runtime Flow

1. Boot and initialize `NVS`, HX711, watering tasks and Wi-Fi stack.
2. Connect to Wi-Fi in station mode.
3. Start HTTP server.
4. Stay online for `WIFI_ONLINE_WINDOW_MS = 30000`.
5. If watering is active when that window expires, keep running until watering completes or is stopped.
6. Stop HTTP, stop Wi-Fi, disable application GPIOs and enter `deep sleep` until the configured wake interval elapses.

## GPIO

- `HX711_DOUT_PIN = GPIO_NUM_19`
- `HX711_SCK_PIN = GPIO_NUM_3`
- `STATUS_LED_PIN = GPIO_NUM_1`
- `START_PIN = GPIO_NUM_10`

## Important Behavior

- No GPIO hold is used before sleep.
- Application GPIOs are disabled before `deep sleep`.
- Zero capture and sleep mode changes are exposed through HTTP API commands.
- `target_g` means target poured mass in grams. It is not time.
- Missing device config keys are initialized with defaults: `device.type=plant`, `device.name=plant`, `dry_weight_g=0`.
- Operation callbacks are best-effort. Failed callbacks do not roll back local controller state.
- `weight_not_changing` is reported only when useful weight changes by less than `STOPPED_CHANGE_THRESHOLD_G` across `STOPPED_CHANGE_DETECT_WINDOW_MS`, currently `6` seconds.

## HTTP API

### `GET /metrics`

Returns plain text:

```text
useful_weight_g 83
gross_weight_g 533
```

### `GET /constants`

Returns compile-time MCU constants and selected runtime calibration values. Wi-Fi credentials are not exposed.
Runtime values currently include `zero_raw` and `raw_per_gram`.

### `GET /watering`

Returns compact JSON:

```json
{
  "device": {
    "type": "plant",
    "name": "plant_1"
  },
  "watering": {
    "active": false,
    "state": "waiting",
    "last_operation_type": "config",
    "last_operation_status": "completed"
  },
  "config": {
    "target_g": 0.0,
    "tare_weight_g": 450.0,
    "dry_weight_g": 120.0,
    "zero_raw": -123456,
    "raw_per_gram": 214.0,
    "sleep_disabled": true,
    "sleep_interval_min": 15
  },
  "weight": {
    "useful_weight_g": 83.0,
    "gross_weight_g": 533.0,
    "water_used_g": 0.0
  }
}
```

### `POST /config`

Updates config. `device_type`, `name`, `dry_weight_g` and `tare_weight_g` are persisted in `NVS`.
Fields can be sent partially. Every supplied field is validated first, then the resulting config is saved in one `NVS` commit.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback",
  "device_type": "plant",
  "name": "plant_1",
  "tare_weight_g": 450,
  "dry_weight_g": 120
}
```

### `GET /healthz`

Returns `200 OK` with `text/plain` body `ok` when the controller is awake and the HTTP server is alive.

### `POST /watering/start`

Starts watering. Only `tank` devices can start watering.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback",
  "target_g": 120
}
```

`target_g` is the target poured mass in grams.

### `POST /watering/stop`

Requests stop for the current watering cycle. If watering is not active, the request succeeds as a no-op and reports
`no active watering`.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback"
}
```

### `POST /sleep/enable`

Enables deep sleep.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback"
}
```

### `POST /sleep/disable`

Disables deep sleep and keeps the controller awake.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback"
}
```

### `POST /sleep/interval`

Updates the deep sleep wake interval in minutes and persists it in `NVS`. Valid values are `1..50`.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback",
  "minutes": 20
}
```

### `POST /zero`

Captures the latest raw HX711 sample as zero and persists it in `NVS`.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback"
}
```

### `POST /calibration`

Calibrates the HX711 raw-per-gram coefficient from the saved `zero_raw` and the latest raw sample, then persists it in `NVS`.

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "callback_url": "http://192.168.1.10:8080/operations/callback",
  "weight_g": 500
}
```

### Operation Callback

When `callback_url` and `operation_id` are present, firmware posts:

```json
{
  "operation_id": "7a5c5f9e-d909-470f-ae20-46a72b0dbf9d",
  "status": "completed",
  "detail": "config_updated"
}
```

Callback delivery is best-effort.

## Main Files

- [main/main.c](/home/sergei/PyCharmMiscProject/smart-watering/firmware/main/main.c) - boot, sleep path, online window
- [main/hx711.c](/home/sergei/PyCharmMiscProject/smart-watering/firmware/main/hx711.c) - HX711 reading and filtering
- [main/watering.c](/home/sergei/PyCharmMiscProject/smart-watering/firmware/main/watering.c) - tare, zero, LED, watering control
- [main/web.c](/home/sergei/PyCharmMiscProject/smart-watering/firmware/main/web.c) - HTTP API
- [main/wifi_sta.c](/home/sergei/PyCharmMiscProject/smart-watering/firmware/main/wifi_sta.c) - Wi-Fi station connect
- [main/app_config.h](/home/sergei/PyCharmMiscProject/smart-watering/firmware/main/app_config.h) - pins and timing constants

## Build

```bash
. ~/esp/esp-idf/export.sh
cd firmware
idf.py build
```

## Flash

```bash
. ~/esp/esp-idf/export.sh
cd firmware
idf.py -p <PORT> flash monitor
```
