#pragma once

void app_watchdog_init(void);
void app_watchdog_add_current_task(const char *task_name);
void app_watchdog_reset(void);
void app_watchdog_delete_current_task(void);
