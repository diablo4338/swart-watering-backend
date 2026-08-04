#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "app_watchdog.h"
#include "app_config.h"
#include "driver/gpio.h"
#include "hx711.h"
#include "watering.h"
#include "web.h"
#include "wifi_sta.h"

static const char *TAG = "app";
static const uint32_t APP_LOOP_STEP_MS = 200;
static const uint32_t WEB_SUPERVISOR_INTERVAL_MS = 5000;
static const uint32_t WEB_IDLE_RESTART_MS = 10 * 60 * 1000;

static const char *wakeup_cause_to_string(esp_sleep_wakeup_cause_t cause)
{
    switch (cause) {
        case ESP_SLEEP_WAKEUP_UNDEFINED:
            return "power_on";
        case ESP_SLEEP_WAKEUP_TIMER:
            return "timer";
        case ESP_SLEEP_WAKEUP_GPIO:
            return "gpio";
        default:
            return "other";
    }
}

static void log_wakeup_cause(void)
{
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    ESP_LOGI(TAG, "boot wakeup_cause=%s (%d)", wakeup_cause_to_string(cause), (int)cause);
}

static void stop_wifi_stack(void)
{
    esp_err_t err = esp_wifi_disconnect();
    if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_STARTED && err != ESP_ERR_WIFI_NOT_CONNECT) {
        ESP_LOGW(TAG, "esp_wifi_disconnect failed: %s", esp_err_to_name(err));
    }

    err = esp_wifi_stop();
    if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_INIT && err != ESP_ERR_WIFI_NOT_STARTED) {
        ESP_LOGW(TAG, "esp_wifi_stop failed: %s", esp_err_to_name(err));
    }

    err = esp_wifi_deinit();
    if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_INIT) {
        ESP_LOGW(TAG, "esp_wifi_deinit failed: %s", esp_err_to_name(err));
    }
}

static void deactivate_gpio(gpio_num_t pin)
{
    gpio_config_t config = {
        .pin_bit_mask = (1ULL << pin),
        .mode = GPIO_MODE_DISABLE,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    gpio_config(&config);
}

static void configure_sleep_wakeup_sources(void)
{
    esp_err_t err = ESP_OK;
    uint64_t sleep_interval_ms = watering_get_sleep_interval_ms();

    if (sleep_interval_ms > 0) {
        err = esp_sleep_enable_timer_wakeup(sleep_interval_ms * 1000ULL);
    }
    ESP_ERROR_CHECK(err);

    ESP_LOGI(TAG,
             "deep sleep wakeup configured: timer_ms=%llu",
             sleep_interval_ms);
}

static void enter_deep_sleep(void)
{
    web_stop();
    watering_prepare_for_sleep();
    stop_wifi_stack();

    configure_sleep_wakeup_sources();

    deactivate_gpio(START_PIN);
    deactivate_gpio(STATUS_LED_PIN);
    deactivate_gpio(HX711_SCK_PIN);
    deactivate_gpio(HX711_DOUT_PIN);

    esp_sleep_pd_config(ESP_PD_DOMAIN_RC_FAST, ESP_PD_OPTION_OFF);
    ESP_LOGI(TAG, "entering deep sleep");
    esp_deep_sleep_start();
}

static void wait_until_sleep_deadline_or_watering_complete(void)
{
    const TickType_t online_window_ticks = pdMS_TO_TICKS(WIFI_ONLINE_WINDOW_MS);
    const TickType_t step_ticks = pdMS_TO_TICKS(APP_LOOP_STEP_MS);
    const TickType_t start_tick = xTaskGetTickCount();
    bool waiting_for_watering_completion = false;
    bool waiting_for_sleep_reenable = false;
    TickType_t last_web_check_tick = xTaskGetTickCount();
    uint32_t last_wifi_generation = wifi_sta_ip_generation();

    app_watchdog_add_current_task("app_main");

    while (true) {
        app_watchdog_reset();
        TickType_t elapsed_ticks = xTaskGetTickCount() - start_tick;
        bool online_window_elapsed = elapsed_ticks >= online_window_ticks;
        bool watering_active = watering_is_active();
        bool sleep_disabled = watering_is_sleep_disabled();
        bool critical_operation_active = web_has_critical_operation();
        TickType_t now_tick = xTaskGetTickCount();

        if ((now_tick - last_web_check_tick) >= pdMS_TO_TICKS(WEB_SUPERVISOR_INTERVAL_MS)) {
            last_web_check_tick = now_tick;
            uint32_t wifi_generation = wifi_sta_ip_generation();

            if (!wifi_sta_is_connected()) {
                ESP_LOGW(TAG, "web supervisor: wifi disconnected");
            } else if (wifi_generation != last_wifi_generation) {
                last_wifi_generation = wifi_generation;
                ESP_LOGW(TAG, "web supervisor: wifi got new ip, restarting http server");
                if (!web_restart()) {
                    ESP_LOGE(TAG, "web supervisor: restart after wifi reconnect failed, restarting MCU");
                    esp_restart();
                }
            } else if (!web_is_running()) {
                ESP_LOGW(TAG, "web supervisor: server is not running, restarting");
                if (!web_restart()) {
                    ESP_LOGE(TAG, "web supervisor: restart failed, restarting MCU");
                    esp_restart();
                }
            } else if (sleep_disabled && web_seconds_since_last_request() > (WEB_IDLE_RESTART_MS / 1000U)) {
                ESP_LOGI(TAG, "web supervisor: idle restart after %lu seconds",
                         (unsigned long)web_seconds_since_last_request());
                if (!web_restart()) {
                    ESP_LOGE(TAG, "web supervisor: idle restart failed, restarting MCU");
                    esp_restart();
                }
            }
        }

        if (sleep_disabled) {
            if (!waiting_for_sleep_reenable) {
                ESP_LOGI(TAG, "deep sleep disabled, keeping http server online");
                waiting_for_sleep_reenable = true;
            }
            vTaskDelay(step_ticks);
            continue;
        }

        if (critical_operation_active) {
            vTaskDelay(step_ticks);
            continue;
        }

        if (waiting_for_sleep_reenable) {
            ESP_LOGI(TAG, "deep sleep enabled");
            waiting_for_sleep_reenable = false;
        }

        if (!online_window_elapsed) {
            TickType_t remaining_ticks = online_window_ticks - elapsed_ticks;
            vTaskDelay(remaining_ticks < step_ticks ? remaining_ticks : step_ticks);
            continue;
        }

        if (!watering_active) {
            return;
        }

        if (!waiting_for_watering_completion) {
            ESP_LOGI(TAG, "online window elapsed, waiting for watering completion");
            waiting_for_watering_completion = true;
        }

        vTaskDelay(step_ticks);
    }
}

void app_main(void)
{
    bool wifi_connected = false;

    log_wakeup_cause();

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    app_watchdog_init();

    hx711_init();
    watering_init();
    wifi_connected = wifi_sta_init();
    if (!wifi_connected) {
        ESP_LOGW(TAG, "wifi unavailable, sleeping immediately");
        enter_deep_sleep();
    }

    if (!web_start()) {
        ESP_LOGW(TAG, "web server failed to start, sleeping immediately");
        enter_deep_sleep();
    }

    ESP_LOGI(TAG, "application started");
    wait_until_sleep_deadline_or_watering_complete();
    enter_deep_sleep();
}
