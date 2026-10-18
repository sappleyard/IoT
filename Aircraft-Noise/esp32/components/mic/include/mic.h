// INMP441 I2S microphone: raw samples, 1 s levels, and 16-bit audio for streaming.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define MIC_SAMPLE_RATE       16000
#define MIC_BLOCK_SAMPLES     2000                                // 125 ms per block
#define MIC_BLOCKS_PER_SECOND (MIC_SAMPLE_RATE / MIC_BLOCK_SAMPLES)

// Start the I2S driver (pins from menuconfig -> "INMP441 microphone"). Safe to call twice.
esp_err_t mic_init(void);

// Block until n samples are read. Output has the mic's DC offset removed and is scaled so
// +-1.0 is digital full scale.
esp_err_t mic_read(float *out, size_t n);

// Convert samples to 16-bit PCM for the raw audio stream, applying MIC_AUDIO_GAIN_DB.
void mic_to_pcm16(const float *in, int16_t *out, size_t n);

// Accumulates blocks into a 1 s level (energy average) and peak (loudest block).
typedef struct {
    double energy;          // sum of squared samples
    size_t samples;
    double loudest_block;   // highest mean square of any one block
} level_meter_t;

void level_meter_reset(level_meter_t *m);
void level_meter_add(level_meter_t *m, const float *x, size_t n);
float level_meter_level_db(const level_meter_t *m);   // estimated dB SPL, NAN if empty
float level_meter_peak_db(const level_meter_t *m);    // estimated dB SPL, NAN if empty
