#include "watering.h"

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_watchdog.h"
#include "app_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hx711.h"
#include "nvs.h"
#include "web.h"

static const char *TAG = "watering";

#define TARE_NVS_NAMESPACE "watering"
#define TARE_NVS_KEY_WEIGHT "tare_weight"
#define ZERO_NVS_KEY_RAW "zero_raw"
#define RAW_PER_GRAM_NVS_KEY "raw_per_g"
#define SLEEP_DISABLED_NVS_KEY "sleep_disabled"
#define SLEEP_INTERVAL_NVS_KEY "sleep_int_min"
#define DEVICE_TYPE_NVS_KEY "device_type"
#define DEVICE_NAME_NVS_KEY "device_name"
#define DRY_WEIGHT_NVS_KEY "dry_weight"
#define DEFAULT_DEVICE_NAME "plant"

typedef struct {
    float values[WEIGHT_BUFFER_SIZE];
    size_t head;
    size_t count;
    float last_absolute_weight_g;
    int32_t last_raw;
    bool has_last_absolute_weight;
    bool has_last_raw;
    SemaphoreHandle_t mutex;
} weight_buffer_t;

typedef struct {
    bool active;
    bool stop_requested;
    device_type_t device_type;
    watering_state_t state;
    watering_operation_type_t last_operation_type;
    watering_operation_status_t last_operation_status;
    char device_name[32];
    float target_g;
    float tolerance_g;
    float start_weight_g;
    float useful_weight_g;
    float water_used_g;
    float tare_weight_g;
    float dry_weight_g;
    float tare_tolerance_g;
    float gross_weight_g;
    bool led_on;
    bool sleep_disabled;
    uint32_t sleep_interval_min;
    int32_t zero_raw;
    float raw_per_gram;
    char detail[96];
    SemaphoreHandle_t mutex;
} watering_runtime_status_t;

typedef struct {
    float target_g;
    char operation_id[40];
    char callback_url[256];
} watering_control_arg_t;

static weight_buffer_t g_weight_buffer = {
    .values = {0},
    .head = 0,
    .count = 0,
    .last_absolute_weight_g = 0.0f,
    .last_raw = 0,
    .has_last_absolute_weight = false,
    .has_last_raw = false,
    .mutex = NULL,
};

static watering_runtime_status_t g_status = {
    .active = false,
    .stop_requested = false,
    .device_type = DEVICE_TYPE_PLANT,
    .state = WATERING_WAITING,
    .last_operation_type = WATERING_OP_NONE,
    .last_operation_status = WATERING_OP_STATUS_NONE,
    .device_name = DEFAULT_DEVICE_NAME,
    .target_g = 0.0f,
    .tolerance_g = 0.0f,
    .start_weight_g = 0.0f,
    .useful_weight_g = 0.0f,
    .water_used_g = 0.0f,
    .tare_weight_g = DEFAULT_TARE_WEIGHT_G,
    .dry_weight_g = 0.0f,
    .tare_tolerance_g = TARE_TOLERANCE_G,
    .gross_weight_g = 0.0f,
    .led_on = false,
    .sleep_disabled = true,
    .sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN,
    .zero_raw = OFFSET_RAW,
    .raw_per_gram = SCALE_RAW_PER_GRAM,
    .detail = "waiting",
    .mutex = NULL,
};

static void load_tare_config_from_nvs(void)
{
    nvs_handle_t nvs_handle = 0;
    float tare_weight_g = DEFAULT_TARE_WEIGHT_G;
    size_t tare_weight_size = sizeof(tare_weight_g);
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        g_status.tare_weight_g = DEFAULT_TARE_WEIGHT_G;
        return;
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to open NVS for tare load: %s", esp_err_to_name(err));
        g_status.tare_weight_g = DEFAULT_TARE_WEIGHT_G;
        return;
    }

    err = nvs_get_blob(nvs_handle, TARE_NVS_KEY_WEIGHT, &tare_weight_g, &tare_weight_size);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "failed to read tare weight: %s", esp_err_to_name(err));
    }

    nvs_close(nvs_handle);

    g_status.tare_weight_g = tare_weight_g;
}

static void load_zero_raw_from_nvs(void)
{
    nvs_handle_t nvs_handle = 0;
    int32_t zero_raw = OFFSET_RAW;
    size_t zero_raw_size = sizeof(zero_raw);
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        g_status.zero_raw = OFFSET_RAW;
        return;
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to open NVS for zero load: %s", esp_err_to_name(err));
        g_status.zero_raw = OFFSET_RAW;
        return;
    }

    err = nvs_get_blob(nvs_handle, ZERO_NVS_KEY_RAW, &zero_raw, &zero_raw_size);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "failed to read zero raw: %s", esp_err_to_name(err));
    }

    nvs_close(nvs_handle);
    g_status.zero_raw = zero_raw;
}

static void load_raw_per_gram_from_nvs(void)
{
    nvs_handle_t nvs_handle = 0;
    float raw_per_gram = SCALE_RAW_PER_GRAM;
    size_t raw_per_gram_size = sizeof(raw_per_gram);
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        g_status.raw_per_gram = SCALE_RAW_PER_GRAM;
        return;
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to open NVS for scale calibration load: %s", esp_err_to_name(err));
        g_status.raw_per_gram = SCALE_RAW_PER_GRAM;
        return;
    }

    err = nvs_get_blob(nvs_handle, RAW_PER_GRAM_NVS_KEY, &raw_per_gram, &raw_per_gram_size);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "failed to read scale calibration: %s", esp_err_to_name(err));
    }

    nvs_close(nvs_handle);
    if (raw_per_gram == 0.0f || !isfinite(raw_per_gram)) {
        raw_per_gram = SCALE_RAW_PER_GRAM;
    }
    g_status.raw_per_gram = raw_per_gram;
}

static void load_sleep_disabled_from_nvs(void)
{
    nvs_handle_t nvs_handle = 0;
    uint8_t sleep_disabled = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        g_status.sleep_disabled = true;
        return;
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to open NVS for sleep mode load: %s", esp_err_to_name(err));
        g_status.sleep_disabled = true;
        return;
    }

    err = nvs_get_u8(nvs_handle, SLEEP_DISABLED_NVS_KEY, &sleep_disabled);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "failed to read sleep mode: %s", esp_err_to_name(err));
    }

    nvs_close(nvs_handle);
    g_status.sleep_disabled = sleep_disabled != 0;
}

static void load_sleep_interval_from_nvs(void)
{
    nvs_handle_t nvs_handle = 0;
    uint32_t sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        g_status.sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN;
        return;
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to open NVS for sleep interval load: %s", esp_err_to_name(err));
        g_status.sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN;
        return;
    }

    err = nvs_get_u32(nvs_handle, SLEEP_INTERVAL_NVS_KEY, &sleep_interval_min);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "failed to read sleep interval: %s", esp_err_to_name(err));
    }

    nvs_close(nvs_handle);
    if (sleep_interval_min < MIN_DEEP_SLEEP_WAKEUP_INTERVAL_MIN
        || sleep_interval_min > MAX_DEEP_SLEEP_WAKEUP_INTERVAL_MIN) {
        sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN;
    }
    g_status.sleep_interval_min = sleep_interval_min;
}

static void load_device_config_from_nvs(void)
{
    nvs_handle_t nvs_handle = 0;
    uint8_t device_type = DEVICE_TYPE_PLANT;
    char device_name[sizeof(g_status.device_name)] = DEFAULT_DEVICE_NAME;
    float dry_weight_g = 0.0f;
    size_t device_name_size = sizeof(device_name);
    size_t dry_weight_size = sizeof(dry_weight_g);
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        g_status.device_type = DEVICE_TYPE_PLANT;
        snprintf(g_status.device_name, sizeof(g_status.device_name), "%s", DEFAULT_DEVICE_NAME);
        g_status.dry_weight_g = 0.0f;
        return;
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to open NVS for device config load: %s", esp_err_to_name(err));
        g_status.device_type = DEVICE_TYPE_PLANT;
        snprintf(g_status.device_name, sizeof(g_status.device_name), "%s", DEFAULT_DEVICE_NAME);
        g_status.dry_weight_g = 0.0f;
        return;
    }

    err = nvs_get_u8(nvs_handle, DEVICE_TYPE_NVS_KEY, &device_type);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        device_type = DEVICE_TYPE_PLANT;
    } else if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to read device type: %s", esp_err_to_name(err));
        device_type = DEVICE_TYPE_PLANT;
    }

    err = nvs_get_str(nvs_handle, DEVICE_NAME_NVS_KEY, device_name, &device_name_size);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        snprintf(device_name, sizeof(device_name), "%s", DEFAULT_DEVICE_NAME);
    } else if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to read device name: %s", esp_err_to_name(err));
        snprintf(device_name, sizeof(device_name), "%s", DEFAULT_DEVICE_NAME);
    }

    err = nvs_get_blob(nvs_handle, DRY_WEIGHT_NVS_KEY, &dry_weight_g, &dry_weight_size);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        dry_weight_g = 0.0f;
    } else if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to read dry weight: %s", esp_err_to_name(err));
        dry_weight_g = 0.0f;
    }

    nvs_close(nvs_handle);

    if (device_type != DEVICE_TYPE_PLANT && device_type != DEVICE_TYPE_TANK) {
        device_type = DEVICE_TYPE_PLANT;
    }
    if (device_name[0] == '\0') {
        snprintf(device_name, sizeof(device_name), "%s", DEFAULT_DEVICE_NAME);
    }
    if (dry_weight_g < 0.0f) {
        dry_weight_g = 0.0f;
    }

    g_status.device_type = (device_type_t)device_type;
    snprintf(g_status.device_name, sizeof(g_status.device_name), "%s", device_name);
    g_status.dry_weight_g = dry_weight_g;
}

static bool save_tare_config_to_nvs(float tare_weight_g)
{
    nvs_handle_t nvs_handle = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to open NVS for tare save: %s", esp_err_to_name(err));
        return false;
    }

    err = nvs_set_blob(nvs_handle, TARE_NVS_KEY_WEIGHT, &tare_weight_g, sizeof(tare_weight_g));
    if (err == ESP_OK) {
        err = nvs_commit(nvs_handle);
    }

    nvs_close(nvs_handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to save tare config: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

static bool save_zero_raw_to_nvs(int32_t zero_raw)
{
    nvs_handle_t nvs_handle = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to open NVS for zero save: %s", esp_err_to_name(err));
        return false;
    }

    err = nvs_set_blob(nvs_handle, ZERO_NVS_KEY_RAW, &zero_raw, sizeof(zero_raw));
    if (err == ESP_OK) {
        err = nvs_commit(nvs_handle);
    }

    nvs_close(nvs_handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to save zero raw: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

static bool save_raw_per_gram_to_nvs(float raw_per_gram)
{
    nvs_handle_t nvs_handle = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to open NVS for scale calibration save: %s", esp_err_to_name(err));
        return false;
    }

    err = nvs_set_blob(nvs_handle, RAW_PER_GRAM_NVS_KEY, &raw_per_gram, sizeof(raw_per_gram));
    if (err == ESP_OK) {
        err = nvs_commit(nvs_handle);
    }

    nvs_close(nvs_handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to save scale calibration: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

static bool save_sleep_disabled_to_nvs(bool sleep_disabled)
{
    nvs_handle_t nvs_handle = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to open NVS for sleep mode save: %s", esp_err_to_name(err));
        return false;
    }

    err = nvs_set_u8(nvs_handle, SLEEP_DISABLED_NVS_KEY, sleep_disabled ? 1 : 0);
    if (err == ESP_OK) {
        err = nvs_commit(nvs_handle);
    }

    nvs_close(nvs_handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to save sleep mode: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

static bool save_sleep_interval_to_nvs(uint32_t sleep_interval_min)
{
    nvs_handle_t nvs_handle = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to open NVS for sleep interval save: %s", esp_err_to_name(err));
        return false;
    }

    err = nvs_set_u32(nvs_handle, SLEEP_INTERVAL_NVS_KEY, sleep_interval_min);
    if (err == ESP_OK) {
        err = nvs_commit(nvs_handle);
    }

    nvs_close(nvs_handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to save sleep interval: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

static bool save_runtime_config_to_nvs(
    device_type_t device_type,
    const char *device_name,
    float dry_weight_g,
    float tare_weight_g,
    bool has_tare_weight_g
)
{
    nvs_handle_t nvs_handle = 0;
    esp_err_t err = nvs_open(TARE_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to open NVS for runtime config save: %s", esp_err_to_name(err));
        return false;
    }

    if (has_tare_weight_g) {
        err = nvs_set_blob(nvs_handle, TARE_NVS_KEY_WEIGHT, &tare_weight_g, sizeof(tare_weight_g));
    }
    if (err == ESP_OK) {
        err = nvs_set_u8(nvs_handle, DEVICE_TYPE_NVS_KEY, (uint8_t)device_type);
    }
    if (err == ESP_OK) {
        err = nvs_set_str(nvs_handle, DEVICE_NAME_NVS_KEY, device_name);
    }
    if (err == ESP_OK) {
        err = nvs_set_blob(nvs_handle, DRY_WEIGHT_NVS_KEY, &dry_weight_g, sizeof(dry_weight_g));
    }
    if (err == ESP_OK) {
        err = nvs_commit(nvs_handle);
    }

    nvs_close(nvs_handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to save runtime config: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

static void set_status_led(bool on)
{
    gpio_set_level(STATUS_LED_PIN, on ? 1 : 0);

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.led_on = on;
    xSemaphoreGive(g_status.mutex);
}

static void blink_status_led_once(void)
{
    set_status_led(true);
    vTaskDelay(pdMS_TO_TICKS(LED_BLINK_MS));
    set_status_led(false);
}

static void weight_buffer_push(int32_t raw, float absolute_weight_g)
{
    xSemaphoreTake(g_weight_buffer.mutex, portMAX_DELAY);
    g_weight_buffer.values[g_weight_buffer.head] = absolute_weight_g;
    g_weight_buffer.head = (g_weight_buffer.head + 1) % WEIGHT_BUFFER_SIZE;
    if (g_weight_buffer.count < WEIGHT_BUFFER_SIZE) {
        g_weight_buffer.count += 1;
    }
    g_weight_buffer.last_absolute_weight_g = absolute_weight_g;
    g_weight_buffer.last_raw = raw;
    g_weight_buffer.has_last_absolute_weight = true;
    g_weight_buffer.has_last_raw = true;
    xSemaphoreGive(g_weight_buffer.mutex);
}

static bool get_latest_absolute_weight_g(float *out_weight_g)
{
    bool has_value = false;

    xSemaphoreTake(g_weight_buffer.mutex, portMAX_DELAY);
    if (g_weight_buffer.has_last_absolute_weight) {
        *out_weight_g = g_weight_buffer.last_absolute_weight_g;
        has_value = true;
    }
    xSemaphoreGive(g_weight_buffer.mutex);

    return has_value;
}

const char *watering_state_to_string(watering_state_t state)
{
    switch (state) {
        case WATERING_WAITING:
            return "waiting";
        case WATERING_WATERING:
            return "watering";
        default:
            return "unknown";
    }
}

const char *watering_operation_type_to_string(watering_operation_type_t type)
{
    switch (type) {
        case WATERING_OP_NONE:
            return "none";
        case WATERING_OP_START:
            return "start";
        case WATERING_OP_STOP:
            return "stop";
        case WATERING_OP_CONFIG:
            return "config";
        case WATERING_OP_SLEEP:
            return "sleep";
        case WATERING_OP_ZERO:
            return "zero";
        case WATERING_OP_CALIBRATION:
            return "calibration";
        default:
            return "unknown";
    }
}

const char *device_type_to_string(device_type_t type)
{
    switch (type) {
        case DEVICE_TYPE_PLANT:
            return "plant";
        case DEVICE_TYPE_TANK:
            return "tank";
        default:
            return "unknown";
    }
}

bool device_type_from_string(const char *value, device_type_t *type_out)
{
    if (value == NULL || type_out == NULL) {
        return false;
    }

    if (strcmp(value, "plant") == 0) {
        *type_out = DEVICE_TYPE_PLANT;
        return true;
    }

    if (strcmp(value, "tank") == 0) {
        *type_out = DEVICE_TYPE_TANK;
        return true;
    }

    return false;
}

const char *watering_operation_status_to_string(watering_operation_status_t status)
{
    switch (status) {
        case WATERING_OP_STATUS_NONE:
            return "none";
        case WATERING_OP_STATUS_IN_PROGRESS:
            return "in_progress";
        case WATERING_OP_STATUS_COMPLETED:
            return "completed";
        case WATERING_OP_STATUS_STOPPED:
            return "stopped";
        case WATERING_OP_STATUS_FAILED:
            return "failed";
        default:
            return "unknown";
    }
}

static void watering_transition_locked(
    bool active,
    watering_operation_type_t operation_type,
    watering_operation_status_t operation_status,
    const char *detail
)
{
    g_status.active = active;
    g_status.state = active ? WATERING_WATERING : WATERING_WAITING;
    g_status.last_operation_type = operation_type;
    g_status.last_operation_status = operation_status;
    snprintf(g_status.detail, sizeof(g_status.detail), "%s", detail);
    if (!active) {
        g_status.stop_requested = false;
    }
}

static void watering_status_set(
    bool active,
    watering_operation_type_t operation_type,
    watering_operation_status_t operation_status,
    float target_g,
    float tolerance_g,
    float start_weight_g,
    float useful_weight_g,
    float water_used_g,
    const char *detail
)
{
    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.target_g = target_g;
    g_status.tolerance_g = tolerance_g;
    g_status.start_weight_g = start_weight_g;
    g_status.useful_weight_g = useful_weight_g;
    g_status.water_used_g = water_used_g;
    watering_transition_locked(active, operation_type, operation_status, detail);
    xSemaphoreGive(g_status.mutex);
}

static void watering_status_update_useful_weight(float useful_weight_g)
{
    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.useful_weight_g = useful_weight_g;
    if (g_status.active) {
        g_status.water_used_g = fabsf(useful_weight_g - g_status.start_weight_g);
    }
    xSemaphoreGive(g_status.mutex);
}

bool watering_is_active(void)
{
    bool active = false;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    active = g_status.active;
    xSemaphoreGive(g_status.mutex);

    return active;
}

static bool watering_stop_requested(void)
{
    bool stop_requested = false;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    stop_requested = g_status.stop_requested;
    xSemaphoreGive(g_status.mutex);

    return stop_requested;
}

void watering_get_status(watering_status_t *snapshot)
{
    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    snapshot->active = g_status.active;
    snapshot->device_type = g_status.device_type;
    snapshot->state = g_status.state;
    snapshot->last_operation_type = g_status.last_operation_type;
    snapshot->last_operation_status = g_status.last_operation_status;
    snprintf(snapshot->device_name, sizeof(snapshot->device_name), "%s", g_status.device_name);
    snapshot->target_g = g_status.target_g;
    snapshot->tolerance_g = g_status.tolerance_g;
    snapshot->start_weight_g = g_status.start_weight_g;
    snapshot->useful_weight_g = g_status.useful_weight_g;
    snapshot->water_used_g = g_status.water_used_g;
    snapshot->tare_weight_g = g_status.tare_weight_g;
    snapshot->dry_weight_g = g_status.dry_weight_g;
    snapshot->tare_tolerance_g = g_status.tare_tolerance_g;
    snapshot->gross_weight_g = g_status.gross_weight_g;
    snapshot->led_on = g_status.led_on;
    snapshot->sleep_disabled = g_status.sleep_disabled;
    snapshot->sleep_interval_min = g_status.sleep_interval_min;
    snapshot->zero_raw = g_status.zero_raw;
    snapshot->raw_per_gram = g_status.raw_per_gram;
    snprintf(snapshot->detail, sizeof(snapshot->detail), "%s", g_status.detail);
    xSemaphoreGive(g_status.mutex);
}

bool watering_is_sleep_disabled(void)
{
    bool sleep_disabled = false;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    sleep_disabled = g_status.sleep_disabled;
    xSemaphoreGive(g_status.mutex);

    return sleep_disabled;
}

uint64_t watering_get_sleep_interval_ms(void)
{
    uint32_t sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    sleep_interval_min = g_status.sleep_interval_min;
    xSemaphoreGive(g_status.mutex);

    return (uint64_t)sleep_interval_min * 60ULL * 1000ULL;
}

bool watering_request_stop(void)
{
    bool can_stop = false;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    if (g_status.active) {
        g_status.stop_requested = true;
        g_status.last_operation_type = WATERING_OP_STOP;
        g_status.last_operation_status = WATERING_OP_STATUS_IN_PROGRESS;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "stop_requested");
        can_stop = true;
    }
    xSemaphoreGive(g_status.mutex);

    return can_stop;
}

static void set_start_pin_low(void)
{
    gpio_set_level(START_PIN, 0);
}

static float get_tare_weight_g(void)
{
    float tare_weight_g = 0.0f;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    tare_weight_g = g_status.tare_weight_g;
    xSemaphoreGive(g_status.mutex);

    return tare_weight_g;
}

static int32_t get_zero_raw(void)
{
    int32_t zero_raw = OFFSET_RAW;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    zero_raw = g_status.zero_raw;
    xSemaphoreGive(g_status.mutex);

    return zero_raw;
}

static float get_raw_per_gram(void)
{
    float raw_per_gram = SCALE_RAW_PER_GRAM;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    raw_per_gram = g_status.raw_per_gram;
    xSemaphoreGive(g_status.mutex);

    if (raw_per_gram == 0.0f || !isfinite(raw_per_gram)) {
        return SCALE_RAW_PER_GRAM;
    }

    return raw_per_gram;
}

static bool get_latest_raw(int32_t *out_raw)
{
    bool has_value = false;

    xSemaphoreTake(g_weight_buffer.mutex, portMAX_DELAY);
    if (g_weight_buffer.has_last_raw) {
        *out_raw = g_weight_buffer.last_raw;
        has_value = true;
    }
    xSemaphoreGive(g_weight_buffer.mutex);

    return has_value;
}

static bool get_latest_useful_weight_g(float *out_weight_g)
{
    float absolute_weight_g = 0.0f;

    if (!get_latest_absolute_weight_g(&absolute_weight_g)) {
        return false;
    }

    *out_weight_g = absolute_weight_g - get_tare_weight_g();
    return true;
}

static void update_calibration_status(
    int32_t zero_raw,
    const char *detail
)
{
    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.zero_raw = zero_raw;
    if (!g_status.active && g_status.state == WATERING_WAITING) {
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", detail);
    }
    xSemaphoreGive(g_status.mutex);
}

bool watering_capture_zero(void)
{
    int32_t raw = 0;
    int32_t previous_zero_raw = OFFSET_RAW;

    if (watering_is_active()) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_ZERO;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "zero_capture_active");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    if (!get_latest_raw(&raw)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_ZERO;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "zero_capture_no_sample");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    previous_zero_raw = get_zero_raw();

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.zero_raw = raw;
    xSemaphoreGive(g_status.mutex);

    if (!save_zero_raw_to_nvs(raw)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.zero_raw = previous_zero_raw;
        g_status.last_operation_type = WATERING_OP_ZERO;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "zero_capture_save_failed");
        xSemaphoreGive(g_status.mutex);
        ESP_LOGE(TAG, "zero capture failed to persist");
        return false;
    }

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.last_operation_type = WATERING_OP_ZERO;
    g_status.last_operation_status = WATERING_OP_STATUS_COMPLETED;
    snprintf(g_status.detail, sizeof(g_status.detail), "%s", "zero_captured");
    xSemaphoreGive(g_status.mutex);

    ESP_LOGI(TAG,
             "zero captured: raw=%ld previous_zero=%ld",
             (long)raw,
             (long)previous_zero_raw);
    return true;
}

bool watering_calibrate_scale(float known_weight_g)
{
    int32_t raw = 0;
    int32_t zero_raw = OFFSET_RAW;
    float raw_per_gram = SCALE_RAW_PER_GRAM;
    float previous_raw_per_gram = SCALE_RAW_PER_GRAM;

    if (known_weight_g <= 0.0f || !isfinite(known_weight_g)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_CALIBRATION;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "invalid_calibration_weight_g");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    if (watering_is_active()) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_CALIBRATION;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "calibration_active");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    if (!get_latest_raw(&raw)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_CALIBRATION;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "calibration_no_sample");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    zero_raw = get_zero_raw();
    if (raw == zero_raw) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_CALIBRATION;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "calibration_no_delta");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    raw_per_gram = ((float)(raw - zero_raw)) / known_weight_g;
    if (raw_per_gram == 0.0f || !isfinite(raw_per_gram)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_CALIBRATION;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "calibration_invalid_coefficient");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    previous_raw_per_gram = get_raw_per_gram();
    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.raw_per_gram = raw_per_gram;
    xSemaphoreGive(g_status.mutex);

    if (!save_raw_per_gram_to_nvs(raw_per_gram)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.raw_per_gram = previous_raw_per_gram;
        g_status.last_operation_type = WATERING_OP_CALIBRATION;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "calibration_save_failed");
        xSemaphoreGive(g_status.mutex);
        ESP_LOGE(TAG, "scale calibration failed to persist");
        return false;
    }

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.last_operation_type = WATERING_OP_CALIBRATION;
    g_status.last_operation_status = WATERING_OP_STATUS_COMPLETED;
    snprintf(g_status.detail, sizeof(g_status.detail), "%s", "calibrated");
    xSemaphoreGive(g_status.mutex);

    ESP_LOGI(TAG,
             "scale calibrated: raw=%ld zero=%ld known=%.1f g raw_per_gram=%.3f previous=%.3f",
             (long)raw,
             (long)zero_raw,
             (double)known_weight_g,
             (double)raw_per_gram,
             (double)previous_raw_per_gram);
    return true;
}

static bool has_invalid_sensor_weight(float absolute_weight_g)
{
    return absolute_weight_g <= 0.0f;
}

static bool wait_or_stop(uint32_t total_ms)
{
    uint32_t waited_ms = 0;
    const uint32_t step_ms = 100;

    while (waited_ms < total_ms) {
        app_watchdog_reset();
        if (watering_stop_requested()) {
            return false;
        }

        uint32_t current_step_ms = step_ms;
        if ((total_ms - waited_ms) < current_step_ms) {
            current_step_ms = total_ms - waited_ms;
        }

        vTaskDelay(pdMS_TO_TICKS(current_step_ms));
        waited_ms += current_step_ms;
    }

    return true;
}

static void reader_task(void *arg)
{
    (void)arg;

    app_watchdog_add_current_task("reader_task");

    while (true) {
        app_watchdog_reset();
        int32_t raw = 0;
        const char *status = NULL;
        float absolute_weight_g = 0.0f;
        float useful_weight_g = 0.0f;

        if (!hx711_read_raw_stable(READ_SAMPLES, &raw, &status)) {
            if (watering_is_active()) {
                ESP_LOGW(TAG, "read skipped: %s", status);
            }
            vTaskDelay(pdMS_TO_TICKS(READ_INTERVAL_MS));
            continue;
        }

        absolute_weight_g = hx711_raw_to_grams(raw, get_zero_raw(), get_raw_per_gram());
        if (fabsf(absolute_weight_g) < ZERO_DEADBAND_G) {
            absolute_weight_g = 0.0f;
        }
        useful_weight_g = absolute_weight_g - get_tare_weight_g();

        weight_buffer_push(raw, absolute_weight_g);
        watering_status_update_useful_weight(useful_weight_g);

        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.gross_weight_g = absolute_weight_g;
        xSemaphoreGive(g_status.mutex);

        if (watering_is_active()) {
            ESP_LOGI(TAG, "raw=%ld absolute=%.1f g useful=%.1f g status=%s",
                     (long)raw, (double)absolute_weight_g, (double)useful_weight_g, status);
        }

        vTaskDelay(pdMS_TO_TICKS(READ_INTERVAL_MS));
    }
}

bool watering_set_sleep_disabled(bool sleep_disabled)
{
    bool previous_sleep_disabled = false;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    previous_sleep_disabled = g_status.sleep_disabled;
    g_status.sleep_disabled = sleep_disabled;
    if (!g_status.active) {
        g_status.last_operation_type = WATERING_OP_SLEEP;
        g_status.last_operation_status = WATERING_OP_STATUS_COMPLETED;
        snprintf(g_status.detail,
                 sizeof(g_status.detail),
                 "%s",
                 sleep_disabled ? "deep_sleep_disabled" : "deep_sleep_enabled");
    }
    xSemaphoreGive(g_status.mutex);

    if (!save_sleep_disabled_to_nvs(sleep_disabled)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.sleep_disabled = previous_sleep_disabled;
        if (!g_status.active) {
            g_status.last_operation_type = WATERING_OP_SLEEP;
            g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
            snprintf(g_status.detail,
                     sizeof(g_status.detail),
                     "%s",
                     previous_sleep_disabled ? "deep_sleep_disabled" : "deep_sleep_enabled");
        }
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    ESP_LOGI(TAG, "deep sleep %s", sleep_disabled ? "disabled" : "enabled");
    return true;
}

bool watering_set_sleep_interval_min(uint32_t sleep_interval_min)
{
    uint32_t previous_sleep_interval_min = DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN;

    if (sleep_interval_min < MIN_DEEP_SLEEP_WAKEUP_INTERVAL_MIN
        || sleep_interval_min > MAX_DEEP_SLEEP_WAKEUP_INTERVAL_MIN) {
        return false;
    }

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    previous_sleep_interval_min = g_status.sleep_interval_min;
    g_status.sleep_interval_min = sleep_interval_min;
    if (!g_status.active) {
        g_status.last_operation_type = WATERING_OP_SLEEP;
        g_status.last_operation_status = WATERING_OP_STATUS_COMPLETED;
        snprintf(g_status.detail, sizeof(g_status.detail), "sleep_interval_%lu_min", (unsigned long)sleep_interval_min);
    }
    xSemaphoreGive(g_status.mutex);

    if (!save_sleep_interval_to_nvs(sleep_interval_min)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.sleep_interval_min = previous_sleep_interval_min;
        if (!g_status.active) {
            g_status.last_operation_type = WATERING_OP_SLEEP;
            g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
            snprintf(g_status.detail, sizeof(g_status.detail), "%s", "sleep_interval_update_failed");
        }
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    ESP_LOGI(TAG, "deep sleep interval set: %lu min", (unsigned long)sleep_interval_min);
    return true;
}

void watering_prepare_for_sleep(void)
{
    set_start_pin_low();
    set_status_led(false);
}

static void tare_monitor_task(void *arg)
{
    (void)arg;
    float absolute_weight_g = 0.0f;
    float useful_weight_g = 0.0f;
    float tare_weight_g = 0.0f;
    bool last_sensor_weight_valid = false;
    bool has_last_sensor_weight_valid = false;

    app_watchdog_add_current_task("tare_monitor_task");

    while (true) {
        app_watchdog_reset();
        tare_weight_g = get_tare_weight_g();

        if (get_latest_absolute_weight_g(&absolute_weight_g)) {
            useful_weight_g = absolute_weight_g - tare_weight_g;
            bool invalid_sensor_weight = has_invalid_sensor_weight(absolute_weight_g);

            if (invalid_sensor_weight) {
                set_status_led(true);
                update_calibration_status(get_zero_raw(), "sensor_weight_invalid");
                if (!has_last_sensor_weight_valid || last_sensor_weight_valid) {
                    ESP_LOGW(TAG, "sensor weight invalid, absolute=%.1f g useful=%.1f g tare=%.1f g",
                             (double)absolute_weight_g,
                             (double)useful_weight_g,
                             (double)tare_weight_g);
                }
                last_sensor_weight_valid = false;
                has_last_sensor_weight_valid = true;
            } else {
                set_status_led(false);
                update_calibration_status(get_zero_raw(), "sensor_weight_valid");
                if (!has_last_sensor_weight_valid || !last_sensor_weight_valid) {
                    ESP_LOGI(TAG, "sensor weight valid, absolute=%.1f g useful=%.1f g tare=%.1f g",
                             (double)absolute_weight_g,
                             (double)useful_weight_g,
                             (double)tare_weight_g);
                }
                last_sensor_weight_valid = true;
                has_last_sensor_weight_valid = true;
            }
        } else {
            set_status_led(false);
            xSemaphoreTake(g_status.mutex, portMAX_DELAY);
            g_status.gross_weight_g = 0.0f;
            xSemaphoreGive(g_status.mutex);
            last_sensor_weight_valid = false;
            has_last_sensor_weight_valid = true;
        }

        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static void create_required_task(
    TaskFunction_t task_function,
    const char *task_name,
    uint32_t stack_depth,
    UBaseType_t priority
)
{
    if (xTaskCreate(task_function, task_name, stack_depth, NULL, priority, NULL) != pdPASS) {
        ESP_LOGE(TAG, "failed to create required task: %s", task_name);
        esp_restart();
    }
}

static void control_task(void *arg)
{
    watering_control_arg_t *control_arg = (watering_control_arg_t *)arg;
    const float target_g = control_arg->target_g;
    const float tolerance_g = target_g * 0.05f;
    float start_weight_g = 0.0f;
    float previous_weight_g = 0.0f;
    float stopped_change_reference_weight_g = 0.0f;
    TickType_t stopped_change_reference_tick = 0;
    bool has_previous_weight = false;
    bool has_stopped_change_reference = false;
    bool stopped_by_request = false;
    const char *final_callback_status = "error";
    const char *final_callback_detail = "stopped_unexpectedly";
    char operation_id[sizeof(control_arg->operation_id)];
    char callback_url[sizeof(control_arg->callback_url)];

    snprintf(operation_id, sizeof(operation_id), "%s", control_arg->operation_id);
    snprintf(callback_url, sizeof(callback_url), "%s", control_arg->callback_url);
    free(arg);
    app_watchdog_add_current_task("control_task");
    if (!wait_or_stop(CONTROL_START_DELAY_MS)) {
        stopped_by_request = true;
        goto finish;
    }

    while (!get_latest_useful_weight_g(&start_weight_g)) {
        if (watering_stop_requested()) {
            stopped_by_request = true;
            goto finish;
        }
        ESP_LOGW(TAG, "waiting for first weight sample");
        vTaskDelay(pdMS_TO_TICKS(200));
    }

    watering_status_set(
        true,
        WATERING_OP_START,
        WATERING_OP_STATUS_IN_PROGRESS,
        target_g,
        tolerance_g,
        start_weight_g,
        start_weight_g,
        0.0f,
        "watering_started"
    );

    ESP_LOGI(TAG, "control start weight=%.1f g", (double)start_weight_g);
    gpio_set_level(START_PIN, 1);
    ESP_LOGI(TAG, "pin %d -> HIGH", START_PIN);
    web_queue_operation_callback(callback_url, operation_id, "started", "pump_on");

    while (true) {
        float current_weight_g = 0.0f;
        float gap_g = 0.0f;

        if (!wait_or_stop(CONTROL_INTERVAL_MS)) {
            stopped_by_request = true;
            break;
        }

        if (!get_latest_useful_weight_g(&current_weight_g)) {
            watering_status_set(
                false,
                WATERING_OP_START,
                WATERING_OP_STATUS_FAILED,
                target_g,
                tolerance_g,
                start_weight_g,
                has_previous_weight ? previous_weight_g : start_weight_g,
                has_previous_weight ? fabsf(previous_weight_g - start_weight_g) : 0.0f,
                "no_weight_available"
            );
            final_callback_status = "error";
            final_callback_detail = "no_weight_available";
            ESP_LOGE(TAG, "emergency: no weight available");
            break;
        }

        gap_g = fabsf(current_weight_g - start_weight_g);
        watering_status_set(
            true,
            WATERING_OP_START,
            WATERING_OP_STATUS_IN_PROGRESS,
            target_g,
            tolerance_g,
            start_weight_g,
            current_weight_g,
            gap_g,
            "running"
        );
        ESP_LOGI(TAG, "control current=%.1f g gap=%.1f g target=%.1f g tolerance=%.1f g",
                 (double)current_weight_g,
                 (double)gap_g,
                 (double)target_g,
                 (double)tolerance_g);

        if (gap_g >= (target_g - tolerance_g)) {
            watering_status_set(
                false,
                WATERING_OP_START,
                WATERING_OP_STATUS_COMPLETED,
                target_g,
                tolerance_g,
                start_weight_g,
                current_weight_g,
                gap_g,
                "target_reached"
            );
            final_callback_status = "success";
            final_callback_detail = "target_reached";
            ESP_LOGI(TAG, "control target reached");
            break;
        }

        if (!has_stopped_change_reference) {
            stopped_change_reference_weight_g = current_weight_g;
            stopped_change_reference_tick = xTaskGetTickCount();
            has_stopped_change_reference = true;
        } else {
            const float weight_delta_g = fabsf(current_weight_g - stopped_change_reference_weight_g);
            if (weight_delta_g >= STOPPED_CHANGE_THRESHOLD_G) {
                stopped_change_reference_weight_g = current_weight_g;
                stopped_change_reference_tick = xTaskGetTickCount();
            } else if ((xTaskGetTickCount() - stopped_change_reference_tick) >= pdMS_TO_TICKS(STOPPED_CHANGE_DETECT_WINDOW_MS)) {
                watering_status_set(
                    false,
                    WATERING_OP_START,
                    WATERING_OP_STATUS_FAILED,
                    target_g,
                    tolerance_g,
                    start_weight_g,
                    current_weight_g,
                    gap_g,
                    "weight_not_changing"
                );
                final_callback_status = "error";
                final_callback_detail = "weight_not_changing";
                ESP_LOGE(TAG, "emergency: weight did not change enough for %d ms (%.2f g)",
                         STOPPED_CHANGE_DETECT_WINDOW_MS,
                         (double)weight_delta_g);
                break;
            }
        }

        previous_weight_g = current_weight_g;
        has_previous_weight = true;
    }

finish:
    set_start_pin_low();
    if (stopped_by_request) {
        watering_status_set(
            false,
            WATERING_OP_STOP,
            WATERING_OP_STATUS_STOPPED,
            target_g,
            tolerance_g,
            start_weight_g,
            has_previous_weight ? previous_weight_g : start_weight_g,
            has_previous_weight ? fabsf(previous_weight_g - start_weight_g) : 0.0f,
            "stop_requested"
        );
        final_callback_status = "success";
        final_callback_detail = "stop_requested";
    } else if (watering_is_active()) {
        watering_status_set(
            false,
            WATERING_OP_START,
            WATERING_OP_STATUS_FAILED,
            target_g,
            tolerance_g,
            start_weight_g,
            has_previous_weight ? previous_weight_g : start_weight_g,
            has_previous_weight ? fabsf(previous_weight_g - start_weight_g) : 0.0f,
            "stopped_unexpectedly"
        );
        final_callback_status = "error";
        final_callback_detail = "stopped_unexpectedly";
    }
    ESP_LOGI(TAG, "pin %d -> LOW", START_PIN);
    app_watchdog_reset();
    web_post_operation_callback(callback_url, operation_id, final_callback_status, final_callback_detail);
    app_watchdog_reset();
    app_watchdog_delete_current_task();
    vTaskDelete(NULL);
}

void watering_init(void)
{
    gpio_config_t output_config = {
        .pin_bit_mask = (1ULL << START_PIN) | (1ULL << STATUS_LED_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    g_weight_buffer.mutex = xSemaphoreCreateMutex();
    g_status.mutex = xSemaphoreCreateMutex();
    load_tare_config_from_nvs();
    load_zero_raw_from_nvs();
    load_raw_per_gram_from_nvs();
    load_sleep_disabled_from_nvs();
    load_sleep_interval_from_nvs();
    load_device_config_from_nvs();
    gpio_config(&output_config);
    set_start_pin_low();
    set_status_led(false);
    blink_status_led_once();
    create_required_task(reader_task, "reader_task", 4096, 5);
    create_required_task(tare_monitor_task, "tare_monitor_task", 4096, 5);
}

static void set_error_detail(char *error_detail, size_t error_detail_size, const char *detail)
{
    if (error_detail == NULL || error_detail_size == 0) {
        return;
    }
    snprintf(error_detail, error_detail_size, "%s", detail);
}

bool watering_start(
    float target_g,
    const char *operation_id,
    const char *callback_url,
    char *error_detail,
    size_t error_detail_size
)
{
    watering_control_arg_t *target_arg = NULL;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    bool is_tank = g_status.device_type == DEVICE_TYPE_TANK;
    xSemaphoreGive(g_status.mutex);

    if (target_g <= 0.0f) {
        set_error_detail(error_detail, error_detail_size, "invalid_target_g");
        return false;
    }

    if (watering_is_active()) {
        set_error_detail(error_detail, error_detail_size, "watering_already_active");
        return false;
    }

    if (!is_tank) {
        set_error_detail(error_detail, error_detail_size, "device_not_tank");
        return false;
    }

    target_arg = calloc(1, sizeof(*target_arg));
    if (target_arg == NULL) {
        set_error_detail(error_detail, error_detail_size, "no_memory");
        watering_status_set(
            false,
            WATERING_OP_START,
            WATERING_OP_STATUS_FAILED,
            target_g,
            target_g * 0.05f,
            0.0f,
            0.0f,
            0.0f,
            "no_memory"
        );
        return false;
    }

    target_arg->target_g = target_g;
    if (operation_id != NULL) {
        snprintf(target_arg->operation_id, sizeof(target_arg->operation_id), "%s", operation_id);
    }
    if (callback_url != NULL) {
        snprintf(target_arg->callback_url, sizeof(target_arg->callback_url), "%s", callback_url);
    }
    watering_status_set(
        true,
        WATERING_OP_START,
        WATERING_OP_STATUS_IN_PROGRESS,
        target_g,
        target_g * 0.05f,
        0.0f,
        0.0f,
        0.0f,
        "waiting_initial_delay"
    );

    if (xTaskCreate(control_task, "control_task", 4096, target_arg, 5, NULL) != pdPASS) {
        free(target_arg);
        set_error_detail(error_detail, error_detail_size, "task_create_failed");
        watering_status_set(
            false,
            WATERING_OP_START,
            WATERING_OP_STATUS_FAILED,
            target_g,
            target_g * 0.05f,
            0.0f,
            0.0f,
            0.0f,
            "task_create_failed"
        );
        return false;
    }

    return true;
}

bool watering_set_tare_weight(float tare_weight_g)
{
    float previous_tare_weight_g = 0.0f;

    if (tare_weight_g < 0.0f) {
        return false;
    }

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    previous_tare_weight_g = g_status.tare_weight_g;
    g_status.tare_weight_g = tare_weight_g;
    xSemaphoreGive(g_status.mutex);

    if (!save_tare_config_to_nvs(tare_weight_g)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.tare_weight_g = previous_tare_weight_g;
        g_status.last_operation_type = WATERING_OP_CONFIG;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "tare_update_failed");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.last_operation_type = WATERING_OP_CONFIG;
    g_status.last_operation_status = WATERING_OP_STATUS_COMPLETED;
    snprintf(g_status.detail, sizeof(g_status.detail), "%s", "tare_updated");
    xSemaphoreGive(g_status.mutex);

    ESP_LOGI(TAG,
             "tare updated from %.1f g to %.1f g",
             (double)previous_tare_weight_g,
             (double)tare_weight_g);

    return true;
}

bool watering_set_runtime_config(
    device_type_t device_type,
    const char *device_name,
    float tare_weight_g,
    bool has_tare_weight_g,
    float dry_weight_g,
    bool has_dry_weight_g
)
{
    if (device_name == NULL || device_name[0] == '\0') {
        return false;
    }

    if ((has_tare_weight_g && tare_weight_g < 0.0f) || (has_dry_weight_g && dry_weight_g < 0.0f)) {
        return false;
    }

    if (watering_is_active()) {
        return false;
    }

    float final_tare_weight_g = has_tare_weight_g ? tare_weight_g : 0.0f;
    float final_dry_weight_g = has_dry_weight_g ? dry_weight_g : 0.0f;

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    if (!has_tare_weight_g) {
        final_tare_weight_g = g_status.tare_weight_g;
    }
    if (!has_dry_weight_g) {
        final_dry_weight_g = g_status.dry_weight_g;
    }
    xSemaphoreGive(g_status.mutex);

    if (!save_runtime_config_to_nvs(
            device_type,
            device_name,
            final_dry_weight_g,
            final_tare_weight_g,
            has_tare_weight_g)) {
        xSemaphoreTake(g_status.mutex, portMAX_DELAY);
        g_status.last_operation_type = WATERING_OP_CONFIG;
        g_status.last_operation_status = WATERING_OP_STATUS_FAILED;
        snprintf(g_status.detail, sizeof(g_status.detail), "%s", "device_config_update_failed");
        xSemaphoreGive(g_status.mutex);
        return false;
    }

    xSemaphoreTake(g_status.mutex, portMAX_DELAY);
    g_status.device_type = device_type;
    snprintf(g_status.device_name, sizeof(g_status.device_name), "%s", device_name);
    if (has_tare_weight_g) {
        g_status.tare_weight_g = final_tare_weight_g;
    }
    g_status.dry_weight_g = final_dry_weight_g;
    g_status.last_operation_type = WATERING_OP_CONFIG;
    g_status.last_operation_status = WATERING_OP_STATUS_COMPLETED;
    snprintf(g_status.detail, sizeof(g_status.detail), "%s", "config_updated");
    xSemaphoreGive(g_status.mutex);

    ESP_LOGI(TAG,
             "runtime config updated: type=%s name=%s tare=%.1f g dry=%.1f g",
             device_type_to_string(device_type),
             device_name,
             (double)final_tare_weight_g,
             (double)final_dry_weight_g);

    return true;
}
