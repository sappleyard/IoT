#include <math.h>

#include "mic.h"

// The INMP441 reads -26 dBFS at 94 dB SPL, so dB SPL = dBFS + 120. Its dBFS is relative to
// a full-scale sine, whose mean square is 0.5, hence the +3.01 dB when working from mean
// square. This is a datasheet estimate: check it against a phone sound meter and put the
// difference in MIC_CAL_OFFSET_DB on the Pi.
#define SPL_AT_0_DBFS   120.0
#define SINE_RMS_DB     3.0103

static float mean_square_to_spl(double ms)
{
    if (ms < 1e-20) {
        ms = 1e-20;
    }
    return (float)(10.0 * log10(ms) + SINE_RMS_DB + SPL_AT_0_DBFS);
}

void level_meter_reset(level_meter_t *m)
{
    m->energy = 0;
    m->samples = 0;
    m->loudest_block = 0;
}

void level_meter_add(level_meter_t *m, const float *x, size_t n)
{
    if (n == 0) {
        return;
    }
    // Sum the block in float (the ESP32 has a float unit, but double is done in software).
    float e = 0;
    for (size_t i = 0; i < n; i++) {
        e += x[i] * x[i];
    }
    m->energy += e;
    m->samples += n;
    if (e / n > m->loudest_block) {
        m->loudest_block = e / n;
    }
}

float level_meter_level_db(const level_meter_t *m)
{
    return m->samples ? mean_square_to_spl(m->energy / m->samples) : NAN;
}

float level_meter_peak_db(const level_meter_t *m)
{
    return m->samples ? mean_square_to_spl(m->loudest_block) : NAN;
}
