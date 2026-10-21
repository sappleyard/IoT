// Remote node (A or B): measures its mic and reports to the bridge over ESP-NOW.
//
// Two power modes (idf.py menuconfig -> "Node settings"):
//
// - Deep sleep (battery): wake every NODE_HEARTBEAT_S, send a heartbeat report, stay awake
//   NODE_REPLY_WAIT_MS for the bridge's reply, then either listen (reporting every second)
//   or go straight back to sleep. A sleeping node can't receive, so the Pi's commands
//   wait at the bridge until the node checks in.
// - Always on (USB power): listen and report every second, forever.
//
// For NODE_BOOT_AWAKE_S after power-on (or the RST button) the node stays awake and accepts
// "audio on" over USB, which streams its raw audio for recording on a laptop (step 4).
// Once audio is on it stays awake until it's reset.

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "battery.h"
#include "esp_attr.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mic.h"
#include "protocol.h"
#include "radio.h"
#include "sdkconfig.h"
#include "serial_link.h"

// Kept in RTC memory, so they survive deep sleep (but not a power cut or reset).
RTC_DATA_ATTR static uint16_t s_seq;              // gaps = lost packets
RTC_DATA_ATTR static uint64_t s_previous_wakes_us; // time in earlier wakes + sleeps, for uptime
RTC_DATA_ATTR static uint8_t s_bridge_mac[6];
RTC_DATA_ATTR static bool s_bridge_mac_set;

static const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

typedef struct {
    NodeCommand cmd;
    uint8_t from[6];
} command_event_t;

static QueueHandle_t s_commands;
static volatile bool s_audio_on;

// --- Radio ----------------------------------------------------------------------------------

static void on_radio_recv(const uint8_t mac[6], const uint8_t *data, int len, int rssi)
{
    if (len != sizeof(NodeCommand) || ((const NodeCommand *)data)->node_id != CONFIG_NODE_ID) {
        return;
    }
    // Before we know the bridge's MAC (broadcast mode), accept the first bridge that answers.
    bool broadcasting = memcmp(s_bridge_mac, BROADCAST, 6) == 0;
    if (!broadcasting && memcmp(mac, s_bridge_mac, 6) != 0) {
        return;
    }
    command_event_t e;
    memcpy(&e.cmd, data, sizeof e.cmd);
    memcpy(e.from, mac, 6);
    xQueueSend(s_commands, &e, 0);   // never block inside a Wi-Fi callback
}

static void setup_radio(void)
{
    if (!s_bridge_mac_set) {   // first boot: take the bridge's MAC from menuconfig
        if (!radio_parse_mac(CONFIG_NODE_BRIDGE_MAC, s_bridge_mac)) {
            memcpy(s_bridge_mac, BROADCAST, 6);
        }
        s_bridge_mac_set = true;
    }
    s_commands = xQueueCreate(4, sizeof(command_event_t));
    ESP_ERROR_CHECK(radio_init(on_radio_recv, NULL));
    ESP_ERROR_CHECK(radio_add_peer(s_bridge_mac));
}

// Commands arrive from the bridge's MAC: if we were broadcasting, switch to sending to it
// directly (unicast gets ACKs and retries; broadcast doesn't).
static void learn_bridge(const uint8_t mac[6])
{
    if (memcmp(s_bridge_mac, BROADCAST, 6) == 0 && radio_add_peer(mac) == ESP_OK) {
        memcpy(s_bridge_mac, mac, 6);
    }
}

static uint32_t uptime_ms(void)
{
    return (uint32_t)((s_previous_wakes_us + esp_timer_get_time()) / 1000);
}

// Send a report. meter is NULL for a heartbeat, when the node didn't listen.
static void send_report(const level_meter_t *meter)
{
    NodeReport r = {
        .node_id = CONFIG_NODE_ID,
        .seq = s_seq++,
        .uptime_ms = uptime_ms(),
        .level_db = meter ? level_meter_level_db(meter) : NAN,
        .peak_db = meter ? level_meter_peak_db(meter) : NAN,
        .level_dba = meter ? level_meter_level_dba(meter) : NAN,
        .peak_dba = meter ? level_meter_peak_dba(meter) : NAN,
        .p_aircraft = NAN,          // step 5
        .battery_mv = battery_read_mv(),
    };
    radio_send(s_bridge_mac, &r, sizeof r);

    if (serial_link_ready()) {      // plugged into a laptop: show it there too
        char json[256];
        protocol_report_to_json(&r, PROTOCOL_NO_RSSI, json, sizeof json);
        serial_link_print_line("%s", json);
    }
}

// --- Listening ------------------------------------------------------------------------------

// Apply any commands from the bridge to the listening deadline.
static void handle_commands(int64_t *deadline_us, TickType_t wait)
{
    command_event_t e;
    while (xQueueReceive(s_commands, &e, wait) == pdTRUE) {
        wait = 0;
        learn_bridge(e.from);
        if (e.cmd.command == CMD_LISTEN) {
            *deadline_us = esp_timer_get_time() + (int64_t)e.cmd.seconds * 1000000;
        } else if (e.cmd.command == CMD_STOP) {
            *deadline_us = 0;
        }
    }
}

static void handle_serial(void)
{
    char line[SERIAL_LINE_MAX];
    while (serial_link_read_line(line, 0)) {
        if (strcmp(line, "audio on") == 0) {
            s_audio_on = true;
        } else if (strcmp(line, "audio off") == 0) {
            s_audio_on = false;
        }
        serial_link_print_line("{\"type\":\"audio\",\"node\":%d,\"on\":%s}",
                               CONFIG_NODE_ID, s_audio_on ? "true" : "false");
    }
}

static bool keep_listening(int64_t deadline_us)
{
#if !CONFIG_NODE_DEEP_SLEEP
    return true;   // always-on mode
#else
    return s_audio_on || esp_timer_get_time() < deadline_us;
#endif
}

// Measure the mic and report every second until the deadline passes.
static void listen_until(int64_t deadline_us)
{
    static float block[MIC_BLOCK_SAMPLES];
    static int16_t pcm[MIC_BLOCK_SAMPLES];
    if (mic_init() != ESP_OK) {
        return;   // nothing useful to report; sleep and try again next wake
    }
    uint16_t audio_seq = 0;
    static level_meter_t meter;
    level_meter_init(&meter);

    while (keep_listening(deadline_us)) {
        level_meter_reset(&meter);
        for (int b = 0; b < MIC_BLOCKS_PER_SECOND; b++) {
            if (mic_read(block, MIC_BLOCK_SAMPLES) != ESP_OK) {
                break;
            }
            level_meter_add(&meter, block, MIC_BLOCK_SAMPLES);
            if (s_audio_on) {
                mic_to_pcm16(block, pcm, MIC_BLOCK_SAMPLES);
                serial_link_send_audio(CONFIG_NODE_ID, audio_seq++, pcm, MIC_BLOCK_SAMPLES);
            }
        }
        send_report(&meter);
        handle_commands(&deadline_us, 0);
        handle_serial();
    }
}

// --- Sleep ----------------------------------------------------------------------------------

static void go_to_sleep(void)
{
#if CONFIG_NODE_DEEP_SLEEP
    uint64_t sleep_us = (uint64_t)CONFIG_NODE_HEARTBEAT_S * 1000000;
    s_previous_wakes_us += esp_timer_get_time() + sleep_us;
    esp_sleep_enable_timer_wakeup(sleep_us);
    esp_deep_sleep_start();   // wakes up as a fresh boot into app_main
#else
    esp_restart();            // not reached: always-on mode listens forever
#endif
}

void app_main(void)
{
    bool woke_from_sleep = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER;
    if (!woke_from_sleep) {
        // A real power-on: the RTC memory is fresh, and there may be a laptop on the USB port.
        s_seq = 0;
        s_previous_wakes_us = 0;
        s_bridge_mac_set = false;
        serial_link_init();
    }
    setup_radio();
    battery_init();

    int64_t deadline_us = 0;
    if (woke_from_sleep) {
        // Heartbeat: tell the bridge we're alive and hear whether the Pi wants us to listen.
        send_report(NULL);
        handle_commands(&deadline_us, pdMS_TO_TICKS(CONFIG_NODE_REPLY_WAIT_MS));
    } else {
        char mac[RADIO_MAC_STR_LEN], bridge[RADIO_MAC_STR_LEN];
        uint8_t own[6];
        radio_own_mac(own);
        radio_format_mac(own, mac);
        radio_format_mac(s_bridge_mac, bridge);
        serial_link_print_line("{\"type\":\"node\",\"node\":%d,\"mac\":\"%s\",\"bridge\":\"%s\"}",
                               CONFIG_NODE_ID, mac, bridge);
        deadline_us = esp_timer_get_time() + (int64_t)CONFIG_NODE_BOOT_AWAKE_S * 1000000;
    }

    if (keep_listening(deadline_us)) {
        listen_until(deadline_us);
    }
    go_to_sleep();
}
