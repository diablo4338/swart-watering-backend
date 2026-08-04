#ifndef HX711_H
#define HX711_H

#include <stdbool.h>
#include <stdint.h>

void hx711_init(void);
bool hx711_read_raw_stable(int samples, int32_t *out_raw, const char **out_status);
float hx711_raw_to_grams(int32_t raw, int32_t offset_raw, float raw_per_gram);

#endif
