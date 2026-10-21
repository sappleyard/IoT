// INMP441 I2S microphone: raw samples, 1 s levels, and 16-bit audio for streaming.
#pragma once

#include <stdbool.h>
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

// A-weighting filter (see level.c): six first-order sections.
#define A_WEIGHT_SECTIONS 6
typedef struct {
    float b0, b1, a1, x1, y1;
} a_section_t;
typedef struct {
    a_section_t section[A_WEIGHT_SECTIONS];
    float scale;            // makes the gain exactly 0 dB at 1 kHz
} a_weight_t;

// Accumulates blocks into a 1 s level (energy average) and peak (loudest block), both
// unweighted and A-weighted (dB(A): how loud it sounds to people, like a phone sound meter).
typedef struct {
    a_weight_t a;           // filter state carries over between seconds
    double energy;          // sum of squared samples
    double energy_a;        // the same, A-weighted
    size_t samples;
    double loudest_block;   // highest mean square of any one block
    double loudest_block_a;
} level_meter_t;

void level_meter_init(level_meter_t *m);    // once, before the first add
void level_meter_reset(level_meter_t *m);   // start a new second (keeps the filter state)
void level_meter_add(level_meter_t *m, const float *x, size_t n);
float level_meter_level_db(const level_meter_t *m);    // estimated dB SPL, NAN if empty
float level_meter_peak_db(const level_meter_t *m);     // estimated dB SPL, NAN if empty
float level_meter_level_dba(const level_meter_t *m);   // estimated dB(A), NAN if empty
float level_meter_peak_dba(const level_meter_t *m);    // estimated dB(A), NAN if empty
