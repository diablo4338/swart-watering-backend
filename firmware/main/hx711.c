#include "hx711.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "app_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "hx711";

typedef struct {
    int32_t last_good_raw;
    int32_t anomaly_candidate_raw;
    bool has_last_good_raw;
    bool has_anomaly_candidate_raw;
    int anomaly_count;
} hx711_state_t;

static hx711_state_t g_hx711 = {0};

static void hx711_delay_us(uint32_t delay_us)
{
    esp_rom_delay_us(delay_us);
}

static bool hx711_wait_ready(void)
{
    const TickType_t timeout_ticks = pdMS_TO_TICKS(2000);
    const TickType_t start_tick = xTaskGetTickCount();

    while (gpio_get_level(HX711_DOUT_PIN) == 1) {
        if ((xTaskGetTickCount() - start_tick) > timeout_ticks) {
            ESP_LOGE(TAG, "HX711 not ready: DOUT stays HIGH");
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }

    return true;
}

static void hx711_clock_pulse(void)
{
    gpio_set_level(HX711_SCK_PIN, 1);
    hx711_delay_us(1);
    gpio_set_level(HX711_SCK_PIN, 0);
    hx711_delay_us(1);
}

static bool hx711_read_raw_once(int32_t *out_raw)
{
    int32_t value = 0;

    if (!hx711_wait_ready()) {
        return false;
    }

    for (int i = 0; i < 24; ++i) {
        gpio_set_level(HX711_SCK_PIN, 1);
        hx711_delay_us(1);
        value = (value << 1) | gpio_get_level(HX711_DOUT_PIN);
        gpio_set_level(HX711_SCK_PIN, 0);
        hx711_delay_us(1);
    }

    hx711_clock_pulse();

    if (value & 0x800000) {
        value -= 0x1000000;
    }

    *out_raw = value;
    return true;
}

static bool hx711_accept_or_skip_raw(int32_t raw, int32_t *accepted_raw, const char **status)
{
    if (!g_hx711.has_last_good_raw) {
        g_hx711.last_good_raw = raw;
        g_hx711.has_last_good_raw = true;
        *accepted_raw = raw;
        *status = "init";
        return true;
    }

    int32_t diff = raw - g_hx711.last_good_raw;
    if (diff < 0) {
        diff = -diff;
    }

    if (diff <= MAX_RAW_JUMP) {
        g_hx711.last_good_raw = raw;
        g_hx711.has_anomaly_candidate_raw = false;
        g_hx711.anomaly_count = 0;
        *accepted_raw = raw;
        *status = "ok";
        return true;
    }

    if (!g_hx711.has_anomaly_candidate_raw) {
        g_hx711.anomaly_candidate_raw = raw;
        g_hx711.has_anomaly_candidate_raw = true;
        g_hx711.anomaly_count = 1;
        *status = "skip";
        return false;
    }

    int32_t candidate_diff = raw - g_hx711.anomaly_candidate_raw;
    if (candidate_diff < 0) {
        candidate_diff = -candidate_diff;
    }

    if (candidate_diff <= MAX_RAW_JUMP) {
        g_hx711.anomaly_count += 1;
    } else {
        g_hx711.anomaly_candidate_raw = raw;
        g_hx711.anomaly_count = 1;
        *status = "skip";
        return false;
    }

    if (g_hx711.anomaly_count >= ANOMALY_CONFIRM_COUNT) {
        g_hx711.last_good_raw = raw;
        g_hx711.has_anomaly_candidate_raw = false;
        g_hx711.anomaly_count = 0;
        *accepted_raw = raw;
        *status = "accepted new level";
        return true;
    }

    *status = "skip";
    return false;
}

void hx711_init(void)
{
    gpio_config_t output_config = {
        .pin_bit_mask = (1ULL << HX711_SCK_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config_t input_config = {
        .pin_bit_mask = (1ULL << HX711_DOUT_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    gpio_config(&output_config);
    gpio_config(&input_config);
    gpio_set_level(HX711_SCK_PIN, 0);

    vTaskDelay(pdMS_TO_TICKS(200));
    for (int i = 0; i < 3; ++i) {
        int32_t raw = 0;
        (void)hx711_read_raw_once(&raw);
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

bool hx711_read_raw_stable(int samples, int32_t *out_raw, const char **out_status)
{
    int32_t accepted_values[READ_SAMPLES];
    int accepted_count = 0;
    int skipped = 0;
    bool accepted_new_level = false;

    for (int i = 0; i < samples; ++i) {
        int32_t raw = 0;
        int32_t accepted_raw = 0;
        const char *status = NULL;

        if (!hx711_read_raw_once(&raw)) {
            *out_status = "timeout";
            return false;
        }

        if (hx711_accept_or_skip_raw(raw, &accepted_raw, &status)) {
            accepted_values[accepted_count++] = accepted_raw;
        }

        if (status != NULL && strcmp(status, "skip") == 0) {
            skipped += 1;
        } else if (status != NULL && strcmp(status, "accepted new level") == 0) {
            accepted_new_level = true;
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }

    if (accepted_count == 0) {
        *out_status = "skipped";
        return false;
    }

    *out_raw = accepted_values[accepted_count - 1];

    if (accepted_new_level) {
        *out_status = "accepted new level";
    } else if (skipped > 0) {
        *out_status = "ok with skips";
    } else {
        *out_status = "ok";
    }

    return true;
}

float hx711_raw_to_grams(int32_t raw, int32_t offset_raw, float raw_per_gram)
{
    return ((float)(raw - offset_raw)) / raw_per_gram;
}
