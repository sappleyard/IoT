// Bridge: the ESP32 plugged into the Pi by USB.
//
// - Receives NodeReports from the remote nodes over ESP-NOW and prints each to the Pi as a
//   JSON line.
// - Measures its own mic and prints a report for itself (node 0) every second.
// - On "audio on", also streams its raw mic audio to the Pi (step 4).
// - Takes commands from the Pi such as "listen 2 90" and hands each to its node the next
//   time that node checks in (a sleeping node can only hear us just after it reports).
//
// Commands from the Pi, one per line:
//   listen <node> <seconds>   node listens and reports every second for that long
//   stop <node>               node goes back to sleep now
//   audio on | audio off      stream the bridge mic's raw audio
//   status                    print the bridge's MAC and the nodes it has heard from

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mic.h"
#include "protocol.h"
#include "radio.h"
#include "sdkconfig.h"
#include "serial_link.h"

// --- Radio events: the ESP-NOW callbacks only copy into this queue -------------------------

typedef enum { EVT_REPORT, EVT_SENT } event_type_t;

typedef struct {
    event_type_t type;
    uint8_t mac[6];
    int rssi;              // EVT_REPORT
    bool delivered;        // EVT_SENT
    NodeReport report;     // EVT_REPORT
} radio_event_t;

static QueueHandle_t s_events;

static void on_radio_recv(const uint8_t mac[6], const uint8_t *data, int len, int rssi)
{
    if (len != sizeof(NodeReport)) {
        return;
    }
    radio_event_t e = {.type = EVT_REPORT, .rssi = rssi};
    memcpy(e.mac, mac, 6);
    memcpy(&e.report, data, sizeof e.report);
    xQueueSend(s_events, &e, 0);   // never block inside a Wi-Fi callback
}

static void on_radio_sent(const uint8_t mac[6], bool delivered)
{
    radio_event_t e = {.type = EVT_SENT, .delivered = delivered};
    memcpy(e.mac, mac, 6);
    xQueueSend(s_events, &e, 0);
}

// --- Remote nodes: where each one is and what the Pi wants it to do next --------------------

typedef struct {
    bool known;            // heard from since the bridge started
    uint8_t mac[6];
    bool has_pending;
    NodeCommand pending;   // waiting to be delivered
} remote_t;

static remote_t s_remotes[MAX_NODES];   // indexed by node_id; 0 (the bridge) unused

static const char *command_name(uint8_t command)
{
    switch (command) {
    case CMD_LISTEN: return "listen";
    case CMD_STOP:   return "stop";
    default:         return "none";
    }
}

static void print_report(const NodeReport *r, int rssi)
{
    char json[256];
    protocol_report_to_json(r, rssi, json, sizeof json);
    serial_link_print_line("%s", json);
}

static void handle_report(const radio_event_t *e)
{
    const NodeReport *r = &e->report;
    uint8_t id = r->node_id;
    if (id == NODE_ID_BRIDGE || id >= MAX_NODES) {
        return;
    }
    remote_t *node = &s_remotes[id];
    if (!node->known || memcmp(node->mac, e->mac, 6) != 0) {
        char mac[RADIO_MAC_STR_LEN];
        radio_format_mac(e->mac, mac);
        radio_add_peer(e->mac);   // so we can reply
        memcpy(node->mac, e->mac, 6);
        node->known = true;
        serial_link_print_line("{\"type\":\"node_seen\",\"node\":%u,\"mac\":\"%s\"}",
                               (unsigned)id, mac);
    }
    print_report(r, e->rssi);

    // The node stays awake ~100 ms after reporting: reply now if the Pi queued something.
    if (node->has_pending) {
        radio_send(node->mac, &node->pending, sizeof node->pending);
    }
}

static void handle_sent(const radio_event_t *e)
{
    for (int id = 1; id < MAX_NODES; id++) {
        remote_t *node = &s_remotes[id];
        if (node->known && node->has_pending && memcmp(node->mac, e->mac, 6) == 0) {
            if (e->delivered) {   // ESP-NOW got an ACK from the node
                serial_link_print_line(
                    "{\"type\":\"delivered\",\"node\":%d,\"command\":\"%s\",\"seconds\":%u}",
                    id, command_name(node->pending.command), (unsigned)node->pending.seconds);
                node->has_pending = false;
            }
            // Not delivered: keep it and try again after the node's next report.
            return;
        }
    }
}

// --- The bridge's own mic -------------------------------------------------------------------

static volatile bool s_audio_on;

static void mic_task(void *arg)
{
    static float block[MIC_BLOCK_SAMPLES];
    static int16_t pcm[MIC_BLOCK_SAMPLES];
    level_meter_t meter;
    level_meter_reset(&meter);
    uint16_t seq = 0, audio_seq = 0;
    int blocks = 0;

    for (;;) {
        if (mic_read(block, MIC_BLOCK_SAMPLES) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        level_meter_add(&meter, block, MIC_BLOCK_SAMPLES);
        if (s_audio_on) {
            mic_to_pcm16(block, pcm, MIC_BLOCK_SAMPLES);
            serial_link_send_audio(NODE_ID_BRIDGE, audio_seq++, pcm, MIC_BLOCK_SAMPLES);
        }
        if (++blocks == MIC_BLOCKS_PER_SECOND) {
            NodeReport r = {
                .node_id = NODE_ID_BRIDGE,
                .seq = seq++,
                .uptime_ms = (uint32_t)(esp_timer_get_time() / 1000),
                .level_db = level_meter_level_db(&meter),
                .peak_db = level_meter_peak_db(&meter),
                .p_aircraft = NAN,      // step 5
                .battery_mv = 0,        // USB powered
            };
            print_report(&r, PROTOCOL_NO_RSSI);
            level_meter_reset(&meter);
            blocks = 0;
        }
    }
}

// --- Commands from the Pi -------------------------------------------------------------------

static void print_status(void)
{
    uint8_t own[6];
    char mac[RADIO_MAC_STR_LEN];
    radio_own_mac(own);
    radio_format_mac(own, mac);
    serial_link_print_line("{\"type\":\"bridge\",\"mac\":\"%s\",\"channel\":%d,"
                           "\"long_range\":%s,\"audio\":%s}",
                           mac, CONFIG_RADIO_CHANNEL,
#if CONFIG_RADIO_LONG_RANGE
                           "true",
#else
                           "false",
#endif
                           s_audio_on ? "true" : "false");
    for (int id = 1; id < MAX_NODES; id++) {
        if (s_remotes[id].known) {
            radio_format_mac(s_remotes[id].mac, mac);
            serial_link_print_line("{\"type\":\"node_seen\",\"node\":%d,\"mac\":\"%s\","
                                   "\"pending\":\"%s\"}", id, mac,
                                   s_remotes[id].has_pending
                                       ? command_name(s_remotes[id].pending.command) : "none");
        }
    }
}

static void queue_command(unsigned id, node_command_t command, unsigned seconds)
{
    if (id == NODE_ID_BRIDGE || id >= MAX_NODES || seconds > UINT16_MAX) {
        serial_link_print_line("{\"type\":\"error\",\"msg\":\"bad node id or seconds\"}");
        return;
    }
    remote_t *node = &s_remotes[id];
    node->pending = (NodeCommand){.node_id = id, .command = command, .seconds = seconds};
    node->has_pending = true;
    serial_link_print_line("{\"type\":\"queued\",\"node\":%u,\"command\":\"%s\",\"seconds\":%u}",
                           id, command_name(command), seconds);
}

static void handle_pi_command(const char *line)
{
    unsigned id, seconds;
    if (sscanf(line, "listen %u %u", &id, &seconds) == 2) {
        queue_command(id, CMD_LISTEN, seconds);
    } else if (sscanf(line, "stop %u", &id) == 1) {
        queue_command(id, CMD_STOP, 0);
    } else if (strcmp(line, "audio on") == 0) {
        s_audio_on = true;
        serial_link_print_line("{\"type\":\"audio\",\"on\":true}");
    } else if (strcmp(line, "audio off") == 0) {
        s_audio_on = false;
        serial_link_print_line("{\"type\":\"audio\",\"on\":false}");
    } else if (strcmp(line, "status") == 0) {
        print_status();
    } else {
        serial_link_print_line("{\"type\":\"error\",\"msg\":\"unknown command\"}");
    }
}

// --- Main loop ------------------------------------------------------------------------------

void app_main(void)
{
    serial_link_init();
    s_events = xQueueCreate(16, sizeof(radio_event_t));
    ESP_ERROR_CHECK(radio_init(on_radio_recv, on_radio_sent));
    print_status();   // includes the MAC address the remote nodes need

    if (mic_init() == ESP_OK) {
        xTaskCreate(mic_task, "mic", 6144, NULL, 6, NULL);
    } else {
        serial_link_print_line("{\"type\":\"error\",\"msg\":\"mic failed to start, check wiring\"}");
    }

    radio_event_t e;
    char line[SERIAL_LINE_MAX];
    for (;;) {
        if (xQueueReceive(s_events, &e, pdMS_TO_TICKS(20)) == pdTRUE) {
            if (e.type == EVT_REPORT) {
                handle_report(&e);
            } else {
                handle_sent(&e);
            }
        }
        while (serial_link_read_line(line, 0)) {
            handle_pi_command(line);
        }
    }
}
