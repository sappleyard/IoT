#include "serial_link.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_idf_version.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(5, 3, 0)
#error "This firmware needs ESP-IDF 5.3 or newer"
#endif

#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#elif CONFIG_ESP_CONSOLE_UART
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#else
#error "Set the console to USB Serial/JTAG or UART in menuconfig (Component config -> ESP System Settings)"
#endif

static SemaphoreHandle_t s_lock;     // one line at a time on the wire
static QueueHandle_t s_lines;        // received lines, SERIAL_LINE_MAX bytes each

// Audio frame (binary, between the text lines):
//   A5 5A | node u8 | seq u16 | n u16 | n bytes of 8-bit mu-law | CRC-16/CCITT u16
// Little-endian; the CRC covers node..payload. 0xA5 never appears in the text lines, so the
// Pi can tell frames from lines, and the CRC lets it drop any frame damaged on the way.
#define FRAME_HEADER 7
static uint8_t s_frame[FRAME_HEADER + SERIAL_AUDIO_MAX_SAMPLES + 2];

// G.711 mu-law: 8 bits per sample on a log scale, so quiet and loud sounds both keep
// ~38 dB of signal-to-noise. Halves the data, which the CH340 USB chip needs.
static uint8_t linear_to_ulaw(int16_t pcm)
{
    const int bias = 0x84, clip = 32635;
    int sign = (pcm >> 8) & 0x80;
    int x = sign ? -(int)pcm : pcm;
    if (x > clip) {
        x = clip;
    }
    x += bias;
    int exponent = 7;
    for (int mask = 0x4000; (x & mask) == 0 && exponent > 0; mask >>= 1) {
        exponent--;
    }
    int mantissa = (x >> (exponent + 3)) & 0x0F;
    return (uint8_t)~(sign | (exponent << 4) | mantissa);
}

// CRC-16/CCITT-FALSE, the same as Python's binascii.crc_hqx(data, 0xFFFF).
static uint16_t crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

// Hand a whole line or frame to the serial driver in one call. stdio would push it through
// one character (and one lock) at a time, which is far too slow for audio.
static void write_line_raw(const void *data, size_t len)
{
    fflush(stdout);   // anything printf'd earlier (e.g. logs) goes out first
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_write_bytes(data, len, pdMS_TO_TICKS(50));   // drops it if no host reads
#else
    uart_write_bytes(CONFIG_ESP_CONSOLE_UART_NUM, data, len);
#endif
}

static void reader_task(void *arg)
{
    char buf[SERIAL_LINE_MAX];
    for (;;) {
        if (fgets(buf, sizeof buf, stdin) == NULL) {
            clearerr(stdin);
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        buf[strcspn(buf, "\r\n")] = '\0';
        if (buf[0] != '\0') {
            xQueueSend(s_lines, buf, 0);   // drop it if nobody is reading
        }
    }
}

void serial_link_init(void)
{
    if (s_lock) {
        return;
    }
    // Install the driver and route stdin/stdout through it, so reads block properly
    // and printf from several tasks doesn't trample over the hardware FIFO.
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.rx_buffer_size = 1024;
    cfg.tx_buffer_size = 8192;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
    usb_serial_jtag_vfs_use_driver();
    usb_serial_jtag_vfs_set_rx_line_endings(ESP_LINE_ENDINGS_LF);
    usb_serial_jtag_vfs_set_tx_line_endings(ESP_LINE_ENDINGS_LF);
#else
    ESP_ERROR_CHECK(uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 1024, 8192, 0, NULL, 0));
    uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
    uart_vfs_dev_port_set_rx_line_endings(CONFIG_ESP_CONSOLE_UART_NUM, ESP_LINE_ENDINGS_LF);
    uart_vfs_dev_port_set_tx_line_endings(CONFIG_ESP_CONSOLE_UART_NUM, ESP_LINE_ENDINGS_LF);
#endif
    setvbuf(stdin, NULL, _IONBF, 0);

    s_lines = xQueueCreate(4, SERIAL_LINE_MAX);
    s_lock = xSemaphoreCreateMutex();
    xTaskCreate(reader_task, "serial_rx", 3072, NULL, 5, NULL);
}

bool serial_link_ready(void)
{
    return s_lock != NULL;
}

void serial_link_print_line(const char *fmt, ...)
{
    if (!s_lock) {
        return;
    }
    char line[320];
    va_list ap;
    va_start(ap, fmt);
    int len = vsnprintf(line, sizeof line - 1, fmt, ap);
    va_end(ap);
    if (len < 0) {
        return;
    }
    if (len > (int)sizeof line - 2) {
        len = sizeof line - 2;   // truncated: still end the line
    }
    line[len++] = '\n';

    xSemaphoreTake(s_lock, portMAX_DELAY);
    write_line_raw(line, len);
    xSemaphoreGive(s_lock);
}

bool serial_link_read_line(char out[SERIAL_LINE_MAX], TickType_t wait)
{
    return s_lines && xQueueReceive(s_lines, out, wait) == pdTRUE;
}

void serial_link_send_audio(uint8_t node_id, uint16_t seq, const int16_t *pcm, size_t samples)
{
    if (!s_lock || samples > SERIAL_AUDIO_MAX_SAMPLES) {
        return;
    }
    uint8_t *f = s_frame;
    f[0] = 0xA5;
    f[1] = 0x5A;
    f[2] = node_id;
    f[3] = seq & 0xFF;
    f[4] = seq >> 8;
    f[5] = samples & 0xFF;
    f[6] = samples >> 8;
    for (size_t i = 0; i < samples; i++) {
        f[FRAME_HEADER + i] = linear_to_ulaw(pcm[i]);
    }
    uint16_t crc = crc16(f + 2, FRAME_HEADER - 2 + samples);
    f[FRAME_HEADER + samples] = crc & 0xFF;
    f[FRAME_HEADER + samples + 1] = crc >> 8;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    write_line_raw(f, FRAME_HEADER + samples + 2);
    xSemaphoreGive(s_lock);
}
