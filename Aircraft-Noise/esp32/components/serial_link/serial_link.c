#include "serial_link.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_idf_version.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
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

// Base64 output for the biggest audio chunk.
static unsigned char s_b64[4 * ((SERIAL_AUDIO_MAX_SAMPLES * 2 + 2) / 3) + 1];

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
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);

    xSemaphoreTake(s_lock, portMAX_DELAY);
    fputs(line, stdout);
    fputc('\n', stdout);
    fflush(stdout);
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
    size_t len = 0;
    // Little-endian int16 straight from memory: exactly what a WAV file holds.
    if (mbedtls_base64_encode(s_b64, sizeof s_b64, &len, (const unsigned char *)pcm,
                              samples * sizeof(int16_t)) != 0) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    printf("A %u %u ", (unsigned)node_id, (unsigned)seq);
    fwrite(s_b64, 1, len, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    xSemaphoreGive(s_lock);
}
