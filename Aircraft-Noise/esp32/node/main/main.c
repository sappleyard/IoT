// Node: measures its mic and sends one LevelsPacket a second to the bridge, for ever.
//
// Mains powered, so it never sleeps. It restarts itself if the main loop hangs (task
// watchdog) or if the bridge hasn't acknowledged a packet for NO_ACK_RESTART_S. Both nodes
// run this same firmware: the Pi tells them apart by MAC address.

#include <stdio.h>

#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "mic.h"
#include "protocol.h"
#include "radio.h"

#define NO_ACK_RESTART_S 60

static volatile int64_t s_last_ack_us;

static void on_sent(bool delivered)
{
    if (delivered) {   // ESP-NOW got an acknowledgement from the bridge
        s_last_ack_us = esp_timer_get_time();
    }
}

void app_main(void)
{
    radio_init(NULL, on_sent);
    ESP_ERROR_CHECK(mic_init());
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    s_last_ack_us = esp_timer_get_time();

    uint16_t seq = 0;
    mic_levels_t m;
    for (;;) {
        ESP_ERROR_CHECK(mic_measure_second(&m));   // blocks for one second

        LevelsPacket p = {
            .version = PROTOCOL_VERSION,
            .flags = m.fault ? FLAG_MIC_FAULT : 0,
            .seq = seq++,
            .uptime_ms = (uint32_t)(esp_timer_get_time() / 1000),
            .laeq = m.laeq,
            .lamax = m.lamax,
        };
        for (int b = 0; b < NUM_BANDS; b++) {
            p.bands[b] = m.bands[b];
        }
        radio_send(BRIDGE_MAC, &p, sizeof p);

        // Shown on the USB serial port, for checking a node plugged into a computer.
        printf("seq %u  LAeq %.1f  LAmax %.1f  bands %.0f %.0f %.0f %.0f %.0f %.0f %.0f%s\n",
               (unsigned)p.seq, (double)m.laeq, (double)m.lamax, (double)m.bands[0],
               (double)m.bands[1], (double)m.bands[2], (double)m.bands[3], (double)m.bands[4],
               (double)m.bands[5], (double)m.bands[6], m.fault ? "  MIC FAULT" : "");

        esp_task_wdt_reset();
        if (esp_timer_get_time() - s_last_ack_us > NO_ACK_RESTART_S * 1000000LL) {
            printf("no acknowledgement from the bridge for %d s: restarting\n", NO_ACK_RESTART_S);
            esp_restart();
        }
    }
}
