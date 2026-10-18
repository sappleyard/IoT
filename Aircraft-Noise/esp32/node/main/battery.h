// Optional battery voltage reading through a resistor divider on an ADC pin.
#pragma once

#include <stdint.h>

void battery_init(void);

// Battery voltage in mV, or 0 if battery measuring is off in menuconfig.
uint16_t battery_read_mv(void);
