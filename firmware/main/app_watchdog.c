#include "app_watchdog.h"

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "app_watchdog";
static const uint32_t APP_TASK_WATCHDOG_TIMEOUT_MS = 10000;
#define APP_TASK_WATCHDOG_IDLE_CORE_MASK ((1U << CONFIG_FREERTOS_NUMBER_OF_CORES) - 1U)

static bool configure_watchdog(void)
{
    esp_task_wdt_config_t config = {
        .timeout_ms = APP_TASK_WATCHDOG_TIMEOUT_MS,
        .idle_core_mask = APP_TASK_WATCHDOG_IDLE_CORE_MASK,
        .trigger_panic = true,
    };

    esp_err_t err = esp_task_wdt_init(&config);
    if (err == ESP_ERR_INVALID_STATE) {
        err = esp_task_wdt_reconfigure(&config);
    }

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "failed to configure task watchdog: %s", esp_err_to_name(err));
        return false;
    }

    return true;
}

void app_watchdog_init(void)
{
    if (configure_watchdog()) {
        ESP_LOGI(TAG, "task watchdog configured: timeout=%lu ms panic=true",
                 (unsigned long)APP_TASK_WATCHDOG_TIMEOUT_MS);
    }
}

void app_watchdog_add_current_task(const char *task_name)
{
    esp_err_t status = esp_task_wdt_status(NULL);
    if (status == ESP_OK) {
        return;
    }

    if (status == ESP_ERR_INVALID_STATE && !configure_watchdog()) {
        return;
    }

    esp_err_t err = esp_task_wdt_add(NULL);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "watching task: %s", task_name != NULL ? task_name : pcTaskGetName(NULL));
    } else {
        ESP_LOGE(TAG, "failed to watch task %s: %s",
                 task_name != NULL ? task_name : pcTaskGetName(NULL),
                 esp_err_to_name(err));
    }
}

void app_watchdog_reset(void)
{
    esp_err_t err = esp_task_wdt_reset();
    if (err != ESP_OK && err != ESP_ERR_NOT_FOUND && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "task watchdog reset failed: %s", esp_err_to_name(err));
    }
}

void app_watchdog_delete_current_task(void)
{
    esp_err_t err = esp_task_wdt_delete(NULL);
    if (err != ESP_OK && err != ESP_ERR_NOT_FOUND && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "task watchdog delete failed: %s", esp_err_to_name(err));
    }
}
