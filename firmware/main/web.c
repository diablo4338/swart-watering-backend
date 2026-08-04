#include "web.h"

#include <stdbool.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_watchdog.h"
#include "app_config.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "watering.h"

static const char *TAG = "web";
static httpd_handle_t g_server = NULL;
static QueueHandle_t g_callback_queue = NULL;
static TaskHandle_t g_callback_task = NULL;
static volatile TickType_t g_last_request_tick = 0;
static volatile bool g_critical_operation_active = false;

#define CALLBACK_URL_SIZE 256
#define REQUEST_BODY_SIZE 512
#define CALLBACK_QUEUE_LENGTH 8
#define CALLBACK_TASK_STACK_SIZE 4096
#define WEB_MAX_OPEN_SOCKETS 4
#define REQUEST_BODY_MAX_TIMEOUTS 3

typedef struct {
    char callback_url[CALLBACK_URL_SIZE];
    char operation_id[40];
    char status[16];
    char detail[64];
} operation_callback_t;

static void mark_request_seen(void)
{
    g_last_request_tick = xTaskGetTickCount();
}

static bool parse_float_value(const char *body, const char *field_name, float *value_out)
{
    char *end_ptr = NULL;
    float parsed_value = 0.0f;
    const char *field_ptr = strstr(body, field_name);
    const char *value_start = body;

    if (field_ptr != NULL) {
        const char *separator_ptr = strchr(field_ptr, ':');
        if (separator_ptr == NULL) {
            separator_ptr = strchr(field_ptr, '=');
        }
        if (separator_ptr != NULL) {
            value_start = separator_ptr + 1;
        }
    }

    while (*value_start == ' ' || *value_start == '\t') {
        value_start++;
    }

    parsed_value = strtof(value_start, &end_ptr);
    if (end_ptr == NULL || end_ptr == value_start) {
        return false;
    }

    *value_out = parsed_value;
    return true;
}

static bool parse_string_value(const char *body, const char *field_name, char *value_out, size_t value_out_size)
{
    const char *field_ptr = strstr(body, field_name);
    const char *value_start = NULL;
    const char *value_end = NULL;
    size_t value_len = 0;

    if (field_ptr == NULL || value_out == NULL || value_out_size == 0) {
        return false;
    }

    value_start = strchr(field_ptr, ':');
    if (value_start == NULL) {
        value_start = strchr(field_ptr, '=');
    }

    if (value_start == NULL) {
        return false;
    }

    value_start++;
    while (*value_start == ' ' || *value_start == '\t' || *value_start == '"') {
        value_start++;
    }

    value_end = value_start;
    while (*value_end != '\0'
           && *value_end != '"'
           && *value_end != ','
           && *value_end != '}'
           && *value_end != '&'
           && *value_end != '\r'
           && *value_end != '\n') {
        value_end++;
    }

    while (value_end > value_start && (*(value_end - 1) == ' ' || *(value_end - 1) == '\t')) {
        value_end--;
    }

    value_len = (size_t)(value_end - value_start);
    if (value_len == 0 || value_len >= value_out_size) {
        return false;
    }

    memcpy(value_out, value_start, value_len);
    value_out[value_len] = '\0';
    return true;
}

static bool body_has_field(const char *body, const char *field_name)
{
    char json_key[40];
    char form_key[40];

    snprintf(json_key, sizeof(json_key), "\"%s\"", field_name);
    snprintf(form_key, sizeof(form_key), "%s=", field_name);

    return strstr(body, json_key) != NULL || strstr(body, form_key) != NULL;
}

static bool parse_target_g(const char *body, float *target_g)
{
    if (!parse_float_value(body, "target_g", target_g)) {
        return false;
    }

    return *target_g > 0.0f;
}

static bool parse_tare_weight_g(const char *body, float *tare_weight_g)
{
    if (parse_float_value(body, "tare_weight_g", tare_weight_g)) {
        return *tare_weight_g >= 0.0f;
    }

    return false;
}

static bool parse_dry_weight_g(const char *body, float *dry_weight_g)
{
    if (parse_float_value(body, "dry_weight_g", dry_weight_g)) {
        return *dry_weight_g >= 0.0f;
    }

    return false;
}

static bool parse_calibration_weight_g(const char *body, float *weight_g)
{
    if (parse_float_value(body, "weight_g", weight_g)) {
        return *weight_g > 0.0f && isfinite(*weight_g);
    }

    return false;
}

static bool parse_sleep_interval_min(const char *body, uint32_t *sleep_interval_min)
{
    char *end_ptr = NULL;
    unsigned long parsed_value = 0;
    const char *field_ptr = strstr(body, "minutes");
    const char *value_start = body;

    if (field_ptr != NULL) {
        const char *separator_ptr = strchr(field_ptr, ':');
        if (separator_ptr == NULL) {
            separator_ptr = strchr(field_ptr, '=');
        }
        if (separator_ptr != NULL) {
            value_start = separator_ptr + 1;
        }
    }

    while (*value_start == ' ' || *value_start == '\t') {
        value_start++;
    }

    parsed_value = strtoul(value_start, &end_ptr, 10);
    if (end_ptr == NULL || end_ptr == value_start) {
        return false;
    }
    while (*end_ptr == ' ' || *end_ptr == '\t') {
        end_ptr++;
    }
    if (*end_ptr != '\0'
        && *end_ptr != ','
        && *end_ptr != '}'
        && *end_ptr != '&'
        && *end_ptr != '\r'
        && *end_ptr != '\n') {
        return false;
    }
    if (parsed_value < MIN_DEEP_SLEEP_WAKEUP_INTERVAL_MIN
        || parsed_value > MAX_DEEP_SLEEP_WAKEUP_INTERVAL_MIN) {
        return false;
    }

    *sleep_interval_min = (uint32_t)parsed_value;
    return true;
}

static bool read_request_body(httpd_req_t *req, char *body, size_t body_size)
{
    if (req->content_len <= 0 || req->content_len >= (int)body_size) {
        return false;
    }

    int total_received = 0;
    int timeout_count = 0;
    while (total_received < req->content_len) {
        int received = httpd_req_recv(req, body + total_received, req->content_len - total_received);
        if (received <= 0) {
            if (received == HTTPD_SOCK_ERR_TIMEOUT) {
                timeout_count++;
                if (timeout_count <= REQUEST_BODY_MAX_TIMEOUTS) {
                    continue;
                }
                ESP_LOGW(TAG, "request body read timed out after %d retries", timeout_count);
            }
            return false;
        }
        timeout_count = 0;
        total_received += received;
    }

    body[total_received] = '\0';
    return true;
}

static bool parse_optional_operation_body(
    httpd_req_t *req,
    char *operation_id,
    size_t operation_id_size,
    char *callback_url,
    size_t callback_url_size
)
{
    char body[REQUEST_BODY_SIZE] = "";

    if (req->content_len <= 0) {
        return true;
    }

    if (!read_request_body(req, body, sizeof(body))) {
        return false;
    }

    parse_string_value(body, "operation_id", operation_id, operation_id_size);
    parse_string_value(body, "callback_url", callback_url, callback_url_size);
    return true;
}

static void callback_task(void *arg)
{
    (void)arg;
    operation_callback_t callback = {0};

    app_watchdog_add_current_task("callback_task");

    while (true) {
        app_watchdog_reset();
        if (xQueueReceive(g_callback_queue, &callback, pdMS_TO_TICKS(1000)) == pdTRUE) {
            web_post_operation_callback(
                callback.callback_url,
                callback.operation_id,
                callback.status,
                callback.detail
            );
            app_watchdog_reset();
        }
    }
}

static bool ensure_callback_worker(void)
{
    if (g_callback_queue == NULL) {
        g_callback_queue = xQueueCreate(CALLBACK_QUEUE_LENGTH, sizeof(operation_callback_t));
        if (g_callback_queue == NULL) {
            ESP_LOGE(TAG, "failed to create callback queue");
            return false;
        }
    }

    if (g_callback_task == NULL) {
        if (xTaskCreate(callback_task, "callback_task", CALLBACK_TASK_STACK_SIZE, NULL, 5, &g_callback_task) != pdPASS) {
            ESP_LOGE(TAG, "failed to create callback task");
            return false;
        }
    }

    return true;
}

void web_queue_operation_callback(const char *callback_url, const char *operation_id, const char *status, const char *detail)
{
    operation_callback_t callback = {0};

    if (callback_url == NULL || callback_url[0] == '\0' || operation_id == NULL || operation_id[0] == '\0') {
        ESP_LOGW(TAG, "operation callback skipped: missing callback_url or operation_id");
        return;
    }

    if (!ensure_callback_worker()) {
        ESP_LOGW(TAG, "operation callback queue unavailable, sending synchronously");
        web_post_operation_callback(callback_url, operation_id, status, detail);
        return;
    }

    snprintf(callback.callback_url, sizeof(callback.callback_url), "%s", callback_url);
    snprintf(callback.operation_id, sizeof(callback.operation_id), "%s", operation_id);
    snprintf(callback.status, sizeof(callback.status), "%s", status != NULL ? status : "error");
    snprintf(callback.detail, sizeof(callback.detail), "%s", detail != NULL ? detail : "");

    if (xQueueSend(g_callback_queue, &callback, 0) != pdTRUE) {
        ESP_LOGW(TAG, "operation callback queue full, dropping operation_id=%s status=%s",
                 callback.operation_id,
                 callback.status);
    }
}

void web_post_operation_callback(const char *callback_url, const char *operation_id, const char *status, const char *detail)
{
    if (callback_url == NULL || callback_url[0] == '\0' || operation_id == NULL || operation_id[0] == '\0') {
        ESP_LOGW(TAG, "operation callback skipped: missing callback_url or operation_id");
        return;
    }

    char payload[256];
    snprintf(
        payload,
        sizeof(payload),
        "{\"operation_id\":\"%s\",\"status\":\"%s\",\"detail\":\"%s\"}",
        operation_id,
        status != NULL ? status : "error",
        detail != NULL ? detail : ""
    );

    esp_http_client_config_t config = {
        .url = callback_url,
        .timeout_ms = 2000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        ESP_LOGW(TAG, "callback client init failed");
        return;
    }

    esp_http_client_set_method(client, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "Connection", "close");
    esp_http_client_set_post_field(client, payload, strlen(payload));

    ESP_LOGI(TAG, "operation callback POST url=%s payload=%s", callback_url, payload);
    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "operation callback failed: url=%s error=%s", callback_url, esp_err_to_name(err));
    } else {
        int status_code = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "operation callback completed: url=%s status=%d", callback_url, status_code);
    }

    esp_http_client_cleanup(client);
}

static esp_err_t metrics_get_handler(httpd_req_t *req)
{
    mark_request_seen();

    char response[96];
    watering_status_t snapshot = {0};
    long useful_weight_g = 0;
    long gross_weight_g = 0;

    watering_get_status(&snapshot);
    useful_weight_g = lroundf(snapshot.useful_weight_g);
    gross_weight_g = lroundf(snapshot.gross_weight_g);

    snprintf(
        response,
        sizeof(response),
        "useful_weight_g %ld\n"
        "gross_weight_g %ld\n",
        useful_weight_g,
        gross_weight_g
    );

    httpd_resp_set_type(req, "text/plain; version=0.0.4; charset=utf-8");
    return httpd_resp_send(req, response, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t health_get_handler(httpd_req_t *req)
{
    mark_request_seen();

    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    return httpd_resp_sendstr(req, "ok");
}

static esp_err_t constants_get_handler(httpd_req_t *req)
{
    mark_request_seen();

    char response[1536];
    watering_status_t snapshot = {0};

    watering_get_status(&snapshot);

    snprintf(
        response,
        sizeof(response),
        "{"
        "\"pins\":{"
            "\"hx711_dout_pin\":%d,"
            "\"hx711_sck_pin\":%d,"
            "\"status_led_pin\":%d,"
            "\"start_pin\":%d"
        "},"
        "\"weight\":{"
            "\"default_offset_raw\":%ld,"
            "\"default_raw_per_gram\":%.6f,"
            "\"read_samples\":%d,"
            "\"read_interval_ms\":%d,"
            "\"zero_deadband_g\":%.2f,"
            "\"weight_buffer_size\":%d,"
            "\"default_tare_weight_g\":%.2f,"
            "\"tare_tolerance_g\":%.2f"
        "},"
        "\"filtering\":{"
            "\"anomaly_threshold_g\":%.2f,"
            "\"anomaly_confirm_count\":%d,"
            "\"max_raw_jump\":%ld"
        "},"
        "\"watering\":{"
            "\"control_start_delay_ms\":%d,"
            "\"control_interval_ms\":%d,"
            "\"stopped_change_threshold_g\":%.2f,"
            "\"stopped_change_detect_window_ms\":%d"
        "},"
        "\"sleep\":{"
            "\"default_deep_sleep_wakeup_interval_min\":%u,"
            "\"min_deep_sleep_wakeup_interval_min\":%u,"
            "\"max_deep_sleep_wakeup_interval_min\":%u,"
            "\"wifi_online_window_ms\":%d"
        "},"
        "\"ui\":{"
            "\"led_blink_ms\":%d"
        "},"
        "\"runtime\":{"
            "\"zero_raw\":%ld,"
            "\"raw_per_gram\":%.6f"
        "}"
        "}",
        (int)HX711_DOUT_PIN,
        (int)HX711_SCK_PIN,
        (int)STATUS_LED_PIN,
        (int)START_PIN,
        (long)OFFSET_RAW,
        (double)SCALE_RAW_PER_GRAM,
        READ_SAMPLES,
        READ_INTERVAL_MS,
        (double)ZERO_DEADBAND_G,
        WEIGHT_BUFFER_SIZE,
        (double)DEFAULT_TARE_WEIGHT_G,
        (double)TARE_TOLERANCE_G,
        (double)ANOMALY_THRESHOLD_G,
        ANOMALY_CONFIRM_COUNT,
        (long)MAX_RAW_JUMP,
        CONTROL_START_DELAY_MS,
        CONTROL_INTERVAL_MS,
        (double)STOPPED_CHANGE_THRESHOLD_G,
        STOPPED_CHANGE_DETECT_WINDOW_MS,
        (unsigned int)DEFAULT_DEEP_SLEEP_WAKEUP_INTERVAL_MIN,
        (unsigned int)MIN_DEEP_SLEEP_WAKEUP_INTERVAL_MIN,
        (unsigned int)MAX_DEEP_SLEEP_WAKEUP_INTERVAL_MIN,
        WIFI_ONLINE_WINDOW_MS,
        LED_BLINK_MS,
        (long)snapshot.zero_raw,
        (double)snapshot.raw_per_gram
    );

    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, response, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t watering_get_handler(httpd_req_t *req)
{
    mark_request_seen();

    char response[768];
    watering_status_t snapshot = {0};

    watering_get_status(&snapshot);

    snprintf(
        response,
        sizeof(response),
        "{"
        "\"device\":{"
            "\"type\":\"%s\","
            "\"name\":\"%s\""
        "},"
        "\"watering\":{"
            "\"active\":%s,"
            "\"state\":\"%s\","
            "\"last_operation_type\":\"%s\","
            "\"last_operation_status\":\"%s\""
        "},"
        "\"config\":{"
            "\"target_g\":%.2f,"
            "\"tare_weight_g\":%.2f,"
            "\"dry_weight_g\":%.2f,"
            "\"zero_raw\":%ld,"
            "\"raw_per_gram\":%.6f,"
            "\"sleep_disabled\":%s,"
            "\"sleep_interval_min\":%lu"
        "},"
        "\"weight\":{"
            "\"useful_weight_g\":%.2f,"
            "\"gross_weight_g\":%.2f,"
            "\"water_used_g\":%.2f"
        "}"
        "}",
        device_type_to_string(snapshot.device_type),
        snapshot.device_name,
        snapshot.active ? "true" : "false",
        watering_state_to_string(snapshot.state),
        watering_operation_type_to_string(snapshot.last_operation_type),
        watering_operation_status_to_string(snapshot.last_operation_status),
        (double)snapshot.target_g,
        (double)snapshot.tare_weight_g,
        (double)snapshot.dry_weight_g,
        (long)snapshot.zero_raw,
        (double)snapshot.raw_per_gram,
        snapshot.sleep_disabled ? "true" : "false",
        (unsigned long)snapshot.sleep_interval_min,
        (double)snapshot.useful_weight_g,
        (double)snapshot.gross_weight_g,
        (double)snapshot.water_used_g
    );

    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, response, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t watering_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char body[REQUEST_BODY_SIZE];
    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";
    char error_detail[96] = "";
    float target_g = 0.0f;

    if (!read_request_body(req, body, sizeof(body))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    if (!parse_target_g(body, &target_g)) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"invalid_target_g\"}");
    }

    parse_string_value(body, "operation_id", operation_id, sizeof(operation_id));
    parse_string_value(body, "callback_url", callback_url, sizeof(callback_url));

    if (watering_is_active()) {
        httpd_resp_set_status(req, "409 Conflict");
        return httpd_resp_sendstr(req, "{\"error\":\"watering_already_active\"}");
    }

    if (!watering_start(target_g, operation_id, callback_url, error_detail, sizeof(error_detail))) {
        const char *detail = error_detail[0] != '\0' ? error_detail : "watering_start_failed";
        char response[160];

        web_queue_operation_callback(callback_url, operation_id, "error", detail);
        httpd_resp_set_status(req, "500 Internal Server Error");
        snprintf(response, sizeof(response), "{\"error\":\"%s\"}", detail);
        return httpd_resp_sendstr(req, response);
    }

    web_queue_operation_callback(callback_url, operation_id, "received", "accepted");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"accepted\"}");
}

static esp_err_t watering_stop_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char body[REQUEST_BODY_SIZE] = "";
    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";

    if (req->content_len > 0) {
        if (!read_request_body(req, body, sizeof(body))) {
            httpd_resp_set_status(req, "400 Bad Request");
            return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
        }
        parse_string_value(body, "operation_id", operation_id, sizeof(operation_id));
        parse_string_value(body, "callback_url", callback_url, sizeof(callback_url));
    }

    if (!watering_request_stop()) {
        web_queue_operation_callback(callback_url, operation_id, "success", "no active watering");
        httpd_resp_set_type(req, "application/json");
        return httpd_resp_sendstr(req, "{\"status\":\"no_active_watering\"}");
    }

    web_queue_operation_callback(callback_url, operation_id, "success", "stop_requested");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"stop_requested\"}");
}

static esp_err_t sleep_enable_post_handler(httpd_req_t *req)
{
    mark_request_seen();
    g_critical_operation_active = true;

    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";

    if (!parse_optional_operation_body(req, operation_id, sizeof(operation_id), callback_url, sizeof(callback_url))) {
        g_critical_operation_active = false;
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    if (!watering_set_sleep_disabled(false)) {
        g_critical_operation_active = false;
        web_queue_operation_callback(callback_url, operation_id, "error", "sleep_enable_failed");
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"error\":\"sleep_enable_failed\"}");
    }

    /*
     * Enabling deep sleep can make app_main enter sleep immediately when the
     * original online window has already elapsed. Deliver the terminal result
     * synchronously and keep app_main behind the critical-operation barrier
     * until that delivery attempt has completed.
     */
    web_post_operation_callback(callback_url, operation_id, "success", "sleep_enabled");
    g_critical_operation_active = false;
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"sleep_enabled\"}");
}

static esp_err_t sleep_disable_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";

    if (!parse_optional_operation_body(req, operation_id, sizeof(operation_id), callback_url, sizeof(callback_url))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    if (!watering_set_sleep_disabled(true)) {
        web_queue_operation_callback(callback_url, operation_id, "error", "sleep_disable_failed");
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"error\":\"sleep_disable_failed\"}");
    }

    web_queue_operation_callback(callback_url, operation_id, "success", "sleep_disabled");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"sleep_disabled\"}");
}

static esp_err_t sleep_interval_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char body[REQUEST_BODY_SIZE];
    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";
    uint32_t sleep_interval_min = 0;

    if (!read_request_body(req, body, sizeof(body))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    parse_string_value(body, "operation_id", operation_id, sizeof(operation_id));
    parse_string_value(body, "callback_url", callback_url, sizeof(callback_url));

    if (!parse_sleep_interval_min(body, &sleep_interval_min)) {
        web_queue_operation_callback(callback_url, operation_id, "error", "invalid_sleep_interval");
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"invalid_sleep_interval\"}");
    }

    if (!watering_set_sleep_interval_min(sleep_interval_min)) {
        web_queue_operation_callback(callback_url, operation_id, "error", "sleep_interval_update_failed");
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"error\":\"sleep_interval_update_failed\"}");
    }

    web_queue_operation_callback(callback_url, operation_id, "success", "sleep_interval_updated");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"sleep_interval_updated\"}");
}

static esp_err_t zero_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";

    if (!parse_optional_operation_body(req, operation_id, sizeof(operation_id), callback_url, sizeof(callback_url))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    if (!watering_capture_zero()) {
        web_queue_operation_callback(callback_url, operation_id, "error", "zero_capture_failed");
        httpd_resp_set_status(req, "409 Conflict");
        return httpd_resp_sendstr(req, "{\"error\":\"zero_capture_failed\"}");
    }

    web_queue_operation_callback(callback_url, operation_id, "success", "zero_captured");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"zero_captured\"}");
}

static esp_err_t calibration_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char body[REQUEST_BODY_SIZE];
    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";
    float weight_g = 0.0f;

    if (!read_request_body(req, body, sizeof(body))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    parse_string_value(body, "operation_id", operation_id, sizeof(operation_id));
    parse_string_value(body, "callback_url", callback_url, sizeof(callback_url));

    if (!parse_calibration_weight_g(body, &weight_g)) {
        web_queue_operation_callback(callback_url, operation_id, "error", "invalid_calibration_weight_g");
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"invalid_calibration_weight_g\"}");
    }

    if (!watering_calibrate_scale(weight_g)) {
        web_queue_operation_callback(callback_url, operation_id, "error", "calibration_failed");
        httpd_resp_set_status(req, "409 Conflict");
        return httpd_resp_sendstr(req, "{\"error\":\"calibration_failed\"}");
    }

    web_queue_operation_callback(callback_url, operation_id, "success", "calibrated");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"calibrated\"}");
}

static esp_err_t config_post_handler(httpd_req_t *req)
{
    mark_request_seen();

    char body[REQUEST_BODY_SIZE];
    char device_type_value[16] = "";
    char device_name[32] = "";
    char operation_id[40] = "";
    char callback_url[CALLBACK_URL_SIZE] = "";
    device_type_t device_type = DEVICE_TYPE_PLANT;
    float tare_weight_g = 0.0f;
    float dry_weight_g = 0.0f;
    bool has_tare_weight_g = false;
    bool has_dry_weight_g = false;
    bool has_device_type = false;
    bool has_device_name = false;
    watering_status_t snapshot = {0};

    if (!read_request_body(req, body, sizeof(body))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"read_failed\"}");
    }

    parse_string_value(body, "operation_id", operation_id, sizeof(operation_id));
    parse_string_value(body, "callback_url", callback_url, sizeof(callback_url));

    watering_get_status(&snapshot);
    device_type = snapshot.device_type;
    snprintf(device_name, sizeof(device_name), "%s", snapshot.device_name);

    has_device_type = body_has_field(body, "device_type");
    if (has_device_type) {
        if (!parse_string_value(body, "device_type", device_type_value, sizeof(device_type_value))
            || !device_type_from_string(device_type_value, &device_type)) {
            httpd_resp_set_status(req, "400 Bad Request");
            return httpd_resp_sendstr(req, "{\"error\":\"invalid_device_type\"}");
        }
    }

    has_device_name = body_has_field(body, "name");
    if (has_device_name && !parse_string_value(body, "name", device_name, sizeof(device_name))) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"invalid_name\"}");
    }

    has_tare_weight_g = body_has_field(body, "tare_weight_g");
    if (has_tare_weight_g && !parse_tare_weight_g(body, &tare_weight_g)) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"invalid_tare_weight_g\"}");
    }

    has_dry_weight_g = body_has_field(body, "dry_weight_g");
    if (has_dry_weight_g && !parse_dry_weight_g(body, &dry_weight_g)) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"invalid_dry_weight_g\"}");
    }

    if (!has_device_type && !has_device_name && !has_tare_weight_g && !has_dry_weight_g) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"empty_config\"}");
    }

    if (!watering_set_runtime_config(
            device_type,
            device_name,
            tare_weight_g,
            has_tare_weight_g,
            dry_weight_g,
            has_dry_weight_g)) {
        web_queue_operation_callback(callback_url, operation_id, "error", "config_update_failed");
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"error\":\"config_update_failed\"}");
    }

    web_queue_operation_callback(callback_url, operation_id, "success", "config_updated");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"status\":\"config_updated\"}");
}

bool web_start(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 12;
    config.max_open_sockets = WEB_MAX_OPEN_SOCKETS;
    config.lru_purge_enable = true;
    config.recv_wait_timeout = 2;
    config.send_wait_timeout = 2;

    httpd_uri_t metrics_get = {
        .uri = "/metrics",
        .method = HTTP_GET,
        .handler = metrics_get_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t health_get = {
        .uri = "/healthz",
        .method = HTTP_GET,
        .handler = health_get_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t constants_get = {
        .uri = "/constants",
        .method = HTTP_GET,
        .handler = constants_get_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t watering_get = {
        .uri = "/watering",
        .method = HTTP_GET,
        .handler = watering_get_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t watering_start_post = {
        .uri = "/watering/start",
        .method = HTTP_POST,
        .handler = watering_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t watering_stop_post = {
        .uri = "/watering/stop",
        .method = HTTP_POST,
        .handler = watering_stop_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t config_post = {
        .uri = "/config",
        .method = HTTP_POST,
        .handler = config_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t sleep_enable_post = {
        .uri = "/sleep/enable",
        .method = HTTP_POST,
        .handler = sleep_enable_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t sleep_disable_post = {
        .uri = "/sleep/disable",
        .method = HTTP_POST,
        .handler = sleep_disable_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t sleep_interval_post = {
        .uri = "/sleep/interval",
        .method = HTTP_POST,
        .handler = sleep_interval_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t zero_post = {
        .uri = "/zero",
        .method = HTTP_POST,
        .handler = zero_post_handler,
        .user_ctx = NULL,
    };
    httpd_uri_t calibration_post = {
        .uri = "/calibration",
        .method = HTTP_POST,
        .handler = calibration_post_handler,
        .user_ctx = NULL,
    };

    if (!ensure_callback_worker()) {
        return false;
    }

    if (httpd_start(&g_server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "failed to start http server");
        g_server = NULL;
        return false;
    }

    if (httpd_register_uri_handler(g_server, &metrics_get) != ESP_OK
        || httpd_register_uri_handler(g_server, &health_get) != ESP_OK
        || httpd_register_uri_handler(g_server, &constants_get) != ESP_OK
        || httpd_register_uri_handler(g_server, &watering_get) != ESP_OK
        || httpd_register_uri_handler(g_server, &watering_start_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &watering_stop_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &config_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &sleep_enable_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &sleep_disable_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &sleep_interval_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &zero_post) != ESP_OK
        || httpd_register_uri_handler(g_server, &calibration_post) != ESP_OK) {
        ESP_LOGE(TAG, "failed to register http handlers");
        web_stop();
        return false;
    }

    mark_request_seen();
    ESP_LOGI(TAG, "http server started max_open_sockets=%d", WEB_MAX_OPEN_SOCKETS);
    return true;
}

void web_stop(void)
{
    if (g_server == NULL) {
        return;
    }

    if (httpd_stop(g_server) == ESP_OK) {
        ESP_LOGI(TAG, "http server stopped");
    } else {
        ESP_LOGW(TAG, "failed to stop http server cleanly");
    }
    g_server = NULL;
}

bool web_is_running(void)
{
    return g_server != NULL;
}

bool web_has_critical_operation(void)
{
    return g_critical_operation_active;
}

uint32_t web_seconds_since_last_request(void)
{
    TickType_t last_request_tick = g_last_request_tick;
    if (last_request_tick == 0) {
        return UINT32_MAX;
    }
    return (uint32_t)((xTaskGetTickCount() - last_request_tick) / configTICK_RATE_HZ);
}

bool web_restart(void)
{
    ESP_LOGW(TAG, "restarting http server");
    web_stop();
    vTaskDelay(pdMS_TO_TICKS(250));
    return web_start();
}
