// Bridge: the ESP32 plugged into the Pi. Relays every LevelsPacket from the nodes to the Pi
// as one JSON line on USB serial, tagged with the sender's MAC and signal strength:
//
//   {"type":"levels","mac":"28:05:a5:35:e0:ec","rssi":-71,"seq":812,"uptime_ms":812345,
//    "flags":0,"laeq":48.2,"lamax":51.0,"bands":[55.1,52.3,49.0,45.2,41.7,38.0,30.4]}
//
// Once a minute it also prints a heartbeat, so the Pi can tell it's alive:
//
//   {"type":"bridge","mac":"28:05:a5:34:6b:38","uptime_ms":60001,"relayed":118,"rejected":0,"dropped":0}
//
// Only the nodes listed in protocol.h are accepted (their packets are encrypted, and anything
// else is counted as rejected). It restarts itself if the main loop hangs (task watchdog).

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_mac.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "protocol.h"
#include "radio.h"

#define HEARTBEAT_S 60

typedef struct {
    uint8_t mac[6];
    int rssi;
    LevelsPacket p;
} received_t;

static QueueHandle_t s_queue;
static volatile uint32_t s_relayed, s_rejected, s_dropped;

static bool is_node(const uint8_t mac[6])
{
    for (int i = 0; i < NUM_NODES; i++) {
        if (memcmp(mac, NODE_MACS[i], 6) == 0) {
            return true;
        }
    }
    return false;
}

// Runs in the Wi-Fi task: check and copy only.
static void on_recv(const uint8_t mac[6], const uint8_t *data, int len, int rssi)
{
    if (!is_node(mac) || len != sizeof(LevelsPacket) || data[0] != PROTOCOL_VERSION) {
        s_rejected++;   // a stranger, or a node running different firmware
        return;
    }
    received_t r = {.rssi = rssi};
    memcpy(r.mac, mac, 6);
    memcpy(&r.p, data, sizeof r.p);
    if (xQueueSend(s_queue, &r, 0) != pdTRUE) {
        s_dropped++;
    }
}

static void print_levels(const received_t *r)
{
    const LevelsPacket *p = &r->p;
    printf("{\"type\":\"levels\",\"mac\":\"" MACSTR "\",\"rssi\":%d,\"seq\":%u,\"uptime_ms\":%" PRIu32
           ",\"flags\":%u,\"laeq\":%.1f,\"lamax\":%.1f,\"bands\":[%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f]}\n",
           MAC2STR(r->mac), r->rssi, (unsigned)p->seq, (uint32_t)p->uptime_ms, (unsigned)p->flags,
           (double)p->laeq, (double)p->lamax, (double)p->bands[0], (double)p->bands[1],
           (double)p->bands[2], (double)p->bands[3], (double)p->bands[4], (double)p->bands[5],
           (double)p->bands[6]);
}

static void print_heartbeat(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    printf("{\"type\":\"bridge\",\"mac\":\"" MACSTR "\",\"uptime_ms\":%" PRIu32
           ",\"relayed\":%" PRIu32 ",\"rejected\":%" PRIu32 ",\"dropped\":%" PRIu32 "}\n",
           MAC2STR(mac), (uint32_t)(esp_timer_get_time() / 1000), s_relayed, s_rejected, s_dropped);
}

void app_main(void)
{
    s_queue = xQueueCreate(32, sizeof(received_t));
    radio_init(on_recv, NULL);
    for (int i = 0; i < NUM_NODES; i++) {
        radio_add_peer(NODE_MACS[i]);   // needed to decrypt their packets
    }
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    print_heartbeat();

    int64_t next_heartbeat = esp_timer_get_time() + HEARTBEAT_S * 1000000LL;
    received_t r;
    for (;;) {
        if (xQueueReceive(s_queue, &r, pdMS_TO_TICKS(1000)) == pdTRUE) {
            print_levels(&r);
            s_relayed++;
        }
        if (esp_timer_get_time() >= next_heartbeat) {
            print_heartbeat();
            next_heartbeat += HEARTBEAT_S * 1000000LL;
        }
        fflush(stdout);
        esp_task_wdt_reset();
    }
}
