#ifndef WEB_H
#define WEB_H

#include <stdbool.h>
#include <stdint.h>

bool web_start(void);
void web_stop(void);
void web_post_operation_callback(const char *callback_url, const char *operation_id, const char *status, const char *detail);
void web_queue_operation_callback(const char *callback_url, const char *operation_id, const char *status, const char *detail);
bool web_is_running(void);
bool web_has_critical_operation(void);
uint32_t web_seconds_since_last_request(void);
bool web_restart(void);

#endif
