// The ESP-NOW packets the boards send each other. Shared by the bridge and node firmware,
// so both sides always agree on the layout.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define NODE_ID_BRIDGE 0
#define MAX_NODES      8        // node IDs 0..7

// Remote node -> bridge: one per heartbeat or 1 s level. The bridge also makes one for
// its own mic. 29 bytes, well inside ESP-NOW's 250-byte limit. The bridge and the nodes
// must run firmware with the same layout (the size is checked on receipt).
typedef struct __attribute__((packed)) {
    uint8_t  node_id;     // 0 = bridge, 1 = A, 2 = B
    uint16_t seq;         // gaps = lost packets
    uint32_t uptime_ms;
    float    level_db;    // 1 s level, unweighted (NAN on a heartbeat where the node didn't listen)
    float    peak_db;     // loudest 125 ms within that second, unweighted
    float    level_dba;   // 1 s level, A-weighted: how loud it sounds (what a phone meter shows)
    float    peak_dba;    // loudest 125 ms, A-weighted
    float    p_aircraft;  // model output from step 5, 0 to 1 (NAN until there is a model)
    uint16_t battery_mv;  // 0 = not measured
} NodeReport;

typedef enum {
    CMD_NONE   = 0,
    CMD_LISTEN = 1,       // listen and report every second for `seconds`
    CMD_STOP   = 2,       // stop listening and go back to sleep
} node_command_t;

// Bridge -> remote node: sent straight after one of that node's reports, while it's
// still awake to hear it.
typedef struct __attribute__((packed)) {
    uint8_t  node_id;     // who it's for
    uint8_t  command;     // node_command_t
    uint16_t seconds;     // CMD_LISTEN: how long
} NodeCommand;

_Static_assert(sizeof(NodeReport) == 29, "NodeReport layout changed");
_Static_assert(sizeof(NodeCommand) == 4, "NodeCommand layout changed");

#define PROTOCOL_NO_RSSI 0  // pass as rssi when there isn't one (the bridge's own mic)

// Format a report as the JSON line sent to the Pi. Returns the snprintf result.
int protocol_report_to_json(const NodeReport *r, int rssi, char *out, size_t size);
