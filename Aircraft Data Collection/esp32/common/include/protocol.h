// What the nodes send to the bridge, once a second. Shared by both firmwares so they
// always agree on the layout; the bridge drops anything else.
#pragma once

#include <stdint.h>

#define RADIO_CHANNEL 1   // Wi-Fi channel all three boards use (ESP-NOW, long-range mode)

// The boards. Traffic is encrypted, so the bridge and the nodes must know each other in
// advance: a new node needs its MAC added here (and to MICS on the Pi), then a reflash.
static const uint8_t BRIDGE_MAC[6] = {0x28, 0x05, 0xA5, 0x34, 0x6B, 0x38};
#define NUM_NODES 2
static const uint8_t NODE_MACS[NUM_NODES][6] = {
    {0x28, 0x05, 0xA5, 0x35, 0xB6, 0x50},   // node A: living room
    {0x28, 0x05, 0xA5, 0x35, 0xE0, 0xEC},   // node B: bedroom
};

#define PROTOCOL_VERSION 2
#define NUM_BANDS 7       // octave bands centred on 63, 125, 250, 500, 1k, 2k and 4k Hz

#define FLAG_MIC_FAULT 0x01   // the mic sent nothing but one repeated value: dead or disconnected

// One second of sound from one node, as numbers only (no audio ever leaves a node).
// Levels are uncalibrated dB SPL; the Pi adds each mic's calibration offset.
typedef struct __attribute__((packed)) {
    uint8_t  version;          // PROTOCOL_VERSION
    uint8_t  flags;            // FLAG_*
    uint16_t seq;              // +1 every second: gaps = lost packets
    uint32_t uptime_ms;        // since the node started: going backwards = it restarted
    float    laeq;             // A-weighted level over the second, dB(A)
    float    lamax;            // loudest 125 ms of the second, dB(A)
    float    bands[NUM_BANDS]; // unweighted level in each octave band, dB
} LevelsPacket;

_Static_assert(sizeof(LevelsPacket) == 44, "LevelsPacket layout changed: bump PROTOCOL_VERSION");
