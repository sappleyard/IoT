#include "mic.h"

#include <math.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "sdkconfig.h"

static const char *TAG = "mic";

#define RAW_CHUNK        512                  // samples per i2s_channel_read
#define STARTUP_DISCARD  (MIC_SAMPLE_RATE / 4) // INMP441 output settles ~85 ms after the clock starts

static i2s_chan_handle_t s_rx;
static int32_t s_raw[RAW_CHUNK];

// High-pass filter: a 2nd-order Butterworth (12 dB/octave) at CONFIG_MIC_HIGHPASS_HZ.
// It removes the mic's DC offset and the rumble below what the INMP441 can really measure
// (it's rated from 60 Hz): wind, traffic vibration, things knocking the desk. Without it that
// rumble dominates the dB reading. Direct form I, which stays accurate in float at low cut-offs.
static struct {
    float b0, b1, b2, a1, a2;   // coefficients, normalised so a0 = 1
    float x1, x2, y1, y2;       // previous inputs and outputs
} s_hpf;

static void highpass_init(float cutoff_hz)
{
    // Audio EQ Cookbook (R. Bristow-Johnson) high-pass, Q = 1/sqrt(2).
    float w0 = 2.0f * (float)M_PI * cutoff_hz / MIC_SAMPLE_RATE;
    float cosw = cosf(w0);
    float alpha = sinf(w0) / (2.0f * 0.70710678f);
    float a0 = 1.0f + alpha;
    s_hpf.b0 = (1.0f + cosw) / 2.0f / a0;
    s_hpf.b1 = -(1.0f + cosw) / a0;
    s_hpf.b2 = s_hpf.b0;
    s_hpf.a1 = -2.0f * cosw / a0;
    s_hpf.a2 = (1.0f - alpha) / a0;
}

static inline float highpass(float x)
{
    float y = s_hpf.b0 * x + s_hpf.b1 * s_hpf.x1 + s_hpf.b2 * s_hpf.x2
              - s_hpf.a1 * s_hpf.y1 - s_hpf.a2 * s_hpf.y2;
    s_hpf.x2 = s_hpf.x1;
    s_hpf.x1 = x;
    s_hpf.y2 = s_hpf.y1;
    s_hpf.y1 = y;
    return y;
}

esp_err_t mic_init(void)
{
    if (s_rx) {
        return ESP_OK;
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 8;
    chan_cfg.dma_frame_num = 512;   // 8 x 512 frames = 256 ms of buffering
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx), TAG, "i2s_new_channel");

    // 32-bit slots, mono. The INMP441's L/R pin is tied to GND so it talks in the left slot.
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CONFIG_MIC_PIN_SCK,
            .ws = CONFIG_MIC_PIN_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = CONFIG_MIC_PIN_SD,
        },
    };
    // If the levels sit at the noise floor no matter what, try I2S_STD_SLOT_RIGHT here:
    // the original ESP32 is known to swap the slots in mono mode.
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    esp_err_t err = i2s_channel_init_std_mode(s_rx, &std_cfg);
    if (err == ESP_OK) {
        err = i2s_channel_enable(s_rx);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S start failed: %s", esp_err_to_name(err));
        i2s_del_channel(s_rx);
        s_rx = NULL;
        return err;
    }

    highpass_init(CONFIG_MIC_HIGHPASS_HZ);
    // Throw away the start-up samples, but run them through the filter so it has settled.
    float settle[RAW_CHUNK];
    for (int left = STARTUP_DISCARD; left > 0; left -= RAW_CHUNK) {
        mic_read(settle, RAW_CHUNK);
    }
    return ESP_OK;
}

esp_err_t mic_read(float *out, size_t n)
{
    size_t done = 0;
    while (done < n) {
        size_t want = n - done < RAW_CHUNK ? n - done : RAW_CHUNK;
        size_t got_bytes = 0;
        ESP_RETURN_ON_ERROR(i2s_channel_read(s_rx, s_raw, want * sizeof(int32_t), &got_bytes,
                                             portMAX_DELAY), TAG, "i2s read");
        size_t got = got_bytes / sizeof(int32_t);
        for (size_t i = 0; i < got; i++) {
            // The 24-bit sample sits in the top of each 32-bit word: shift it down.
            float x = (float)(s_raw[i] >> 8) * (1.0f / 8388608.0f);
            out[done + i] = highpass(x);
        }
        done += got;
    }
    return ESP_OK;
}

void mic_to_pcm16(const float *in, int16_t *out, size_t n)
{
    // Digital gain so quiet sounds use more than the bottom few bits of the 16-bit stream.
    // The step 5 model must apply the same gain to what it hears.
    const float scale = 32767.0f * powf(10.0f, CONFIG_MIC_AUDIO_GAIN_DB / 20.0f);
    for (size_t i = 0; i < n; i++) {
        float v = in[i] * scale;
        if (v > 32767.0f) {
            v = 32767.0f;
        } else if (v < -32768.0f) {
            v = -32768.0f;
        }
        out[i] = (int16_t)lrintf(v);
    }
}
