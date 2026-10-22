// INMP441 over I2S at 16 kHz -> one second of levels: LAeq, LAmax (125 ms) and seven
// octave bands. The audio itself is only ever held in small buffers and thrown away.

#include "mic.h"

#include <complex.h>
#include <math.h>
#include <stdint.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "mic";

// Wiring: VDD 3V3, GND, L/R to GND (left channel), and:
#define PIN_SCK 26   // bit clock
#define PIN_WS  25   // word select
#define PIN_SD  33   // data

#define SAMPLE_RATE   16000
#define SAMPLES_125MS (SAMPLE_RATE / 8)
#define CHUNK         500        // samples per I2S read; divides 125 ms exactly
#define DC_BLOCK_POLE 0.995f     // removes the mic's DC offset (~13 Hz high-pass)

// The INMP441 reads -26 dBFS at 94 dB SPL, so dB SPL = dBFS + 120 (dBFS relative to a
// full-scale sine, hence +3.01 dB when working from mean square). A datasheet figure: the Pi
// corrects each mic with its calibration offset.
static float mean_square_to_db(double ms)
{
    return (float)(10.0 * log10(ms > 1e-20 ? ms : 1e-20) + 3.0103 + 120.0);
}

// --- Filters ------------------------------------------------------------------------------

typedef struct {
    float b0, b1, b2, a1, a2;
    float x1, x2, y1, y2;
} biquad_t;

static inline float biquad(biquad_t *f, float x)
{
    float y = f->b0 * x + f->b1 * f->x1 + f->b2 * f->x2 - f->a1 * f->y1 - f->a2 * f->y2;
    f->x2 = f->x1;
    f->x1 = x;
    f->y2 = f->y1;
    f->y1 = y;
    return y;
}

// 2nd-order Butterworth high- or low-pass (Audio EQ Cookbook, Q = 1/sqrt(2)).
static biquad_t butterworth(float hz, bool highpass)
{
    float w0 = 2.0f * (float)M_PI * hz / SAMPLE_RATE;
    float cosw = cosf(w0), alpha = sinf(w0) / (2.0f * 0.70710678f), a0 = 1.0f + alpha;
    biquad_t f = {.a1 = -2.0f * cosw / a0, .a2 = (1.0f - alpha) / a0};
    if (highpass) {
        f.b0 = (1.0f + cosw) / 2.0f / a0;
        f.b1 = -(1.0f + cosw) / a0;
    } else {
        f.b0 = (1.0f - cosw) / 2.0f / a0;
        f.b1 = (1.0f - cosw) / a0;
    }
    f.b2 = f.b0;
    return f;
}

// Octave bands: centre fc, edges fc/sqrt(2) and fc*sqrt(2), each a high-pass at the lower
// edge followed by a low-pass at the upper edge. (8 kHz isn't possible at 16 kHz sampling.)
// Simpler than a lab-grade octave filter: for broadband sound (traffic, aircraft) each band
// reads within 0.7 dB of an ideal octave; a pure tone at a band's centre reads ~2 dB low.
static const float BAND_HZ[NUM_BANDS] = {62.5f, 125, 250, 500, 1000, 2000, 4000};
static biquad_t s_band_hp[NUM_BANDS], s_band_lp[NUM_BANDS];

// A-weighting (IEC 61672): the analog curve s^4 / ((s+w1)^2 (s+w2) (s+w3) (s+w4)^2) with
// corners at 20.6, 107.7, 737.9 and 12194 Hz, each factor a first-order section through the
// bilinear transform, scaled to 0 dB at 1 kHz. Within 0.5 dB of the curve up to 4 kHz.
#define A_SECTIONS 6
static const float A_HZ[A_SECTIONS] = {20.598997f, 20.598997f, 107.65265f, 737.86223f,
                                       12194.217f, 12194.217f};
static const bool A_HIGHPASS[A_SECTIONS] = {true, true, true, true, false, false};
static struct {
    float b0, b1, a1, x1, y1;
} s_a[A_SECTIONS];
static float s_a_scale;

static void a_weighting_init(void)
{
    const float k = 2.0f * SAMPLE_RATE;   // bilinear transform: s = k (z - 1) / (z + 1)
    float complex z1 = cexpf(-I * 2.0f * (float)M_PI * 1000.0f / SAMPLE_RATE);   // 1 kHz
    float complex gain = 1.0f;
    for (int i = 0; i < A_SECTIONS; i++) {
        float w = 2.0f * (float)M_PI * A_HZ[i];
        s_a[i].a1 = (w - k) / (k + w);
        s_a[i].b0 = (A_HIGHPASS[i] ? k : w) / (k + w);
        s_a[i].b1 = A_HIGHPASS[i] ? -s_a[i].b0 : s_a[i].b0;
        gain *= (s_a[i].b0 + s_a[i].b1 * z1) / (1.0f + s_a[i].a1 * z1);
    }
    s_a_scale = 1.0f / cabsf(gain);
}

static inline float a_weight(float x)
{
    for (int i = 0; i < A_SECTIONS; i++) {
        float y = s_a[i].b0 * x + s_a[i].b1 * s_a[i].x1 - s_a[i].a1 * s_a[i].y1;
        s_a[i].x1 = x;
        s_a[i].y1 = y;
        x = y;
    }
    return x * s_a_scale;
}

// --- I2S and measuring ----------------------------------------------------------------------

static i2s_chan_handle_t s_rx;
static int32_t s_raw[CHUNK];
static float s_dc_in, s_dc_out;

esp_err_t mic_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 8;
    chan_cfg.dma_frame_num = 500;   // 8 x 500 frames = 250 ms of buffering
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx), TAG, "i2s_new_channel");

    // 32-bit slots, mono, left slot (the INMP441's L/R pin is tied to GND).
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_SCK,
            .ws = PIN_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = PIN_SD,
        },
    };
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx, &std_cfg), TAG, "init_std_mode");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx), TAG, "enable");

    for (int b = 0; b < NUM_BANDS; b++) {
        s_band_hp[b] = butterworth(BAND_HZ[b] / (float)M_SQRT2, true);
        s_band_lp[b] = butterworth(BAND_HZ[b] * (float)M_SQRT2, false);
    }
    a_weighting_init();

    // The INMP441 takes ~85 ms to settle and the filters need a moment: discard one second.
    mic_levels_t ignored;
    return mic_measure_second(&ignored);
}

esp_err_t mic_measure_second(mic_levels_t *out)
{
    double energy_a = 0, loudest_125ms = 0, energy_band[NUM_BANDS] = {0};
    float block_a = 0;              // A-weighted energy of the current 125 ms
    int32_t raw_min = INT32_MAX, raw_max = INT32_MIN;

    for (int done = 0; done < SAMPLE_RATE; done += CHUNK) {
        size_t got = 0;
        ESP_RETURN_ON_ERROR(i2s_channel_read(s_rx, s_raw, sizeof s_raw, &got, portMAX_DELAY),
                            TAG, "i2s read");
        float chunk_band[NUM_BANDS] = {0};
        for (int i = 0; i < CHUNK; i++) {
            int32_t raw = s_raw[i] >> 8;   // the 24-bit sample sits in the top of each word
            if (raw < raw_min) raw_min = raw;
            if (raw > raw_max) raw_max = raw;

            float x = (float)raw * (1.0f / 8388608.0f);
            float y = x - s_dc_in + DC_BLOCK_POLE * s_dc_out;
            s_dc_in = x;
            s_dc_out = y;

            float a = a_weight(y);
            block_a += a * a;
            for (int b = 0; b < NUM_BANDS; b++) {
                float v = biquad(&s_band_lp[b], biquad(&s_band_hp[b], y));
                chunk_band[b] += v * v;
            }
        }
        for (int b = 0; b < NUM_BANDS; b++) {
            energy_band[b] += chunk_band[b];
        }
        if ((done + CHUNK) % SAMPLES_125MS == 0) {   // end of a 125 ms block
            energy_a += block_a;
            if (block_a / SAMPLES_125MS > loudest_125ms) {
                loudest_125ms = block_a / SAMPLES_125MS;
            }
            block_a = 0;
        }
    }

    out->laeq = mean_square_to_db(energy_a / SAMPLE_RATE);
    out->lamax = mean_square_to_db(loudest_125ms);
    for (int b = 0; b < NUM_BANDS; b++) {
        out->bands[b] = mean_square_to_db(energy_band[b] / SAMPLE_RATE);
    }
    out->fault = raw_min == raw_max;
    return ESP_OK;
}
