// ESP-NOW between the boards: Wi-Fi on but connected to nothing, on RADIO_CHANNEL, in
// Espressif's long-range mode (slower bit rate, much better reach through floors and walls).
// Every link is encrypted (AES-CCMP) with the keys in secrets.h, which flash.sh generates.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

// Both callbacks run in the Wi-Fi task: copy what you need and return quickly.
typedef void (*radio_recv_cb_t)(const uint8_t mac[6], const uint8_t *data, int len, int rssi);
typedef void (*radio_sent_cb_t)(bool delivered);

// Start NVS, Wi-Fi and ESP-NOW. Either callback may be NULL.
void radio_init(radio_recv_cb_t on_recv, radio_sent_cb_t on_sent);

// Add a board we talk to, as an encrypted peer. Packets from a peer can only be decrypted
// once it has been added, so the bridge adds every node at start-up.
void radio_add_peer(const uint8_t mac[6]);

// Send to a peer. Delivery (an acknowledgement from the receiver) is reported to on_sent.
esp_err_t radio_send(const uint8_t mac[6], const void *data, size_t len);
