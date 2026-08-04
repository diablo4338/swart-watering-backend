#ifndef WIFI_STA_H
#define WIFI_STA_H

#include <stdbool.h>
#include <stdint.h>

bool wifi_sta_init(void);
bool wifi_sta_is_connected(void);
uint32_t wifi_sta_ip_generation(void);

#endif
