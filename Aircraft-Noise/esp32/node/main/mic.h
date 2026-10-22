// The node's INMP441 microphone, boiled down to a few levels per second.
#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "protocol.h"

typedef struct {
    float laeq;              // A-weighted level over the second, dB(A)
    float lamax;             // loudest 125 ms, dB(A)
    float bands[NUM_BANDS];  // unweighted octave-band levels, dB
    bool fault;              // every raw sample was the same value: mic dead or disconnected
} mic_levels_t;

esp_err_t mic_init(void);

// Listen for one second (blocks) and measure it. Levels are uncalibrated dB SPL.
esp_err_t mic_measure_second(mic_levels_t *out);
