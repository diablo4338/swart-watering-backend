#ifndef WATERING_H
#define WATERING_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

typedef enum {
    WATERING_WAITING = 0,
    WATERING_WATERING,
} watering_state_t;

typedef enum {
    WATERING_OP_NONE = 0,
    WATERING_OP_START,
    WATERING_OP_STOP,
    WATERING_OP_CONFIG,
    WATERING_OP_SLEEP,
    WATERING_OP_ZERO,
    WATERING_OP_CALIBRATION,
} watering_operation_type_t;

typedef enum {
    DEVICE_TYPE_PLANT = 0,
    DEVICE_TYPE_TANK,
} device_type_t;

typedef enum {
    WATERING_OP_STATUS_NONE = 0,
    WATERING_OP_STATUS_IN_PROGRESS,
    WATERING_OP_STATUS_COMPLETED,
    WATERING_OP_STATUS_STOPPED,
    WATERING_OP_STATUS_FAILED,
} watering_operation_status_t;

typedef struct {
    bool active;
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
    float wet_weight_g;
    float watering_loss_threshold_percent;
    float tare_tolerance_g;
    float gross_weight_g;
    bool led_on;
    bool sleep_disabled;
    uint32_t sleep_interval_min;
    int32_t zero_raw;
    float raw_per_gram;
    char detail[96];
} watering_status_t;

void watering_init(void);
bool watering_start(
    float target_g,
    const char *operation_id,
    const char *callback_url,
    char *error_detail,
    size_t error_detail_size
);
bool watering_request_stop(void);
void watering_get_status(watering_status_t *snapshot);
bool watering_is_active(void);
bool watering_is_sleep_disabled(void);
bool watering_set_sleep_disabled(bool sleep_disabled);
uint64_t watering_get_sleep_interval_ms(void);
bool watering_set_sleep_interval_min(uint32_t sleep_interval_min);
bool watering_capture_zero(void);
bool watering_calibrate_scale(float known_weight_g);
bool watering_set_tare_weight(float tare_weight_g);
bool watering_set_runtime_config(
    device_type_t device_type,
    const char *device_name,
    float tare_weight_g,
    bool has_tare_weight_g,
    float dry_weight_g,
    bool has_dry_weight_g,
    float wet_weight_g,
    bool has_wet_weight_g,
    float watering_loss_threshold_percent,
    bool has_watering_loss_threshold_percent
);
void watering_prepare_for_sleep(void);
const char *watering_state_to_string(watering_state_t state);
const char *watering_operation_type_to_string(watering_operation_type_t type);
const char *watering_operation_status_to_string(watering_operation_status_t status);
const char *device_type_to_string(device_type_t type);
bool device_type_from_string(const char *value, device_type_t *type_out);

#endif
