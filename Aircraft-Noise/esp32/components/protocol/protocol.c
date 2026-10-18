#include "protocol.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>

// JSON has no NaN, so missing values become null.
static const char *number(char *buf, size_t size, float v, const char *fmt)
{
    if (isnan(v)) {
        return "null";
    }
    snprintf(buf, size, fmt, (double)v);
    return buf;
}

int protocol_report_to_json(const NodeReport *r, int rssi, char *out, size_t size)
{
    char level[16], peak[16], p[16], rssi_s[8];
    if (rssi == PROTOCOL_NO_RSSI) {
        snprintf(rssi_s, sizeof rssi_s, "null");
    } else {
        snprintf(rssi_s, sizeof rssi_s, "%d", rssi);
    }
    return snprintf(out, size,
                    "{\"type\":\"report\",\"node\":%u,\"seq\":%u,\"uptime_ms\":%" PRIu32
                    ",\"level_db\":%s,\"peak_db\":%s,\"p_aircraft\":%s,\"battery_mv\":%u"
                    ",\"rssi\":%s}",
                    (unsigned)r->node_id, (unsigned)r->seq, (uint32_t)r->uptime_ms,
                    number(level, sizeof level, r->level_db, "%.1f"),
                    number(peak, sizeof peak, r->peak_db, "%.1f"),
                    number(p, sizeof p, r->p_aircraft, "%.3f"),
                    (unsigned)r->battery_mv, rssi_s);
}
