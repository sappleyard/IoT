// Text lines over USB serial between an ESP32 and the Pi (or a laptop).
//
// Outgoing: JSON text lines, plus binary audio frames (mu-law, with a CRC; see
// serial_link.c). Incoming: command lines like "audio on". Works with both the native USB port (ESP32-S3) and
// a USB-UART chip (original ESP32), whichever is the console in sdkconfig.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"

#define SERIAL_LINE_MAX 96          // longest command line accepted
#define SERIAL_AUDIO_MAX_SAMPLES 2048

// Install the serial driver and start reading lines in the background.
void serial_link_init(void);

// False until serial_link_init has run. The print/send functions do nothing until then.
bool serial_link_ready(void);

// Print one whole line (a newline is added). Safe to call from several tasks.
void serial_link_print_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

// Next line received from the host, waiting up to `wait` ticks. False if none.
bool serial_link_read_line(char out[SERIAL_LINE_MAX], TickType_t wait);

// Send a chunk of 16-bit audio as a binary mu-law frame. Call from one task only.
void serial_link_send_audio(uint8_t node_id, uint16_t seq, const int16_t *pcm, size_t samples);
