// ESP-NOW setup shared by the bridge and the nodes: Wi-Fi on but connected to nothing,
// pinned to one channel, so the boards can talk directly with no router.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define RADIO_MAC_STR_LEN 18   // "AA:BB:CC:DD:EE:FF" + NUL

// Called from the Wi-Fi task: copy what you need and return quickly (no slow work here).
typedef void (*radio_recv_cb_t)(const uint8_t mac[6], const uint8_t *data, int len, int rssi);
typedef void (*radio_sent_cb_t)(const uint8_t mac[6], bool delivered);

// Start NVS, Wi-Fi (station mode, not connected) and ESP-NOW on CONFIG_RADIO_CHANNEL.
// Either callback may be NULL.
esp_err_t radio_init(radio_recv_cb_t on_recv, radio_sent_cb_t on_sent);

// Add a peer to send to (does nothing if it's already one).
esp_err_t radio_add_peer(const uint8_t mac[6]);

esp_err_t radio_send(const uint8_t mac[6], const void *data, size_t len);

void radio_own_mac(uint8_t mac[6]);
void radio_format_mac(const uint8_t mac[6], char out[RADIO_MAC_STR_LEN]);
bool radio_parse_mac(const char *str, uint8_t mac[6]);
