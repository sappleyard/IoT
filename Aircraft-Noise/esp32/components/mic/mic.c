#include "mic.h"

#include <math.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "sdkconfig.h"

static const char *TAG = "mic";

#define RAW_CHUNK        512                  // samples per i2s_channel_read
#define DC_BLOCK_POLE    0.995f               // high-pass at ~13 Hz to remove the mic's DC offset
#define STARTUP_DISCARD  (MIC_SAMPLE_RATE / 4) // INMP441 output settles ~85 ms after the clock starts

static i2s_chan_handle_t s_rx;
static int32_t s_raw[RAW_CHUNK];
static float s_prev_in, s_prev_out;

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

    for (int left = STARTUP_DISCARD; left > 0; left -= RAW_CHUNK) {
        size_t got;
        i2s_channel_read(s_rx, s_raw, sizeof s_raw, &got, portMAX_DELAY);
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
            float y = x - s_prev_in + DC_BLOCK_POLE * s_prev_out;
            s_prev_in = x;
            s_prev_out = y;
            out[done + i] = y;
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
