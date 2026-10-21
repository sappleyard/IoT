#include <complex.h>
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

// --- A-weighting --------------------------------------------------------------------------
//
// The IEC 61672 A-weighting curve, which weights frequencies the way ears hear them (deep
// rumble counts for much less). It's the "dB(A)" a phone sound meter shows. The analog curve is
//     H(s) = s^4 / ((s + w1)^2 (s + w2) (s + w3) (s + w4)^2)
// with corner frequencies 20.6, 107.7, 737.9 and 12194 Hz. Each factor becomes a first-order
// digital section through the bilinear transform, and the cascade is scaled to 0 dB at 1 kHz.
// At 16 kHz this is within ~1 dB of the curve up to ~4 kHz, where aircraft noise lives.

static const struct {
    float hz;
    bool highpass;   // s/(s+w), else w/(s+w)
} A_SECTIONS[A_WEIGHT_SECTIONS] = {
    {20.598997f, true}, {20.598997f, true}, {107.65265f, true},
    {737.86223f, true}, {12194.217f, false}, {12194.217f, false},
};

static void a_weight_init(a_weight_t *a)
{
    const float k = 2.0f * MIC_SAMPLE_RATE;   // bilinear transform: s = k (z - 1) / (z + 1)
    const float w1k = 2.0f * (float)M_PI * 1000.0f / MIC_SAMPLE_RATE;
    float complex z1 = cexpf(-I * w1k);       // z^-1 at 1 kHz, to find the gain there
    float complex gain = 1.0f;

    for (int i = 0; i < A_WEIGHT_SECTIONS; i++) {
        float w = 2.0f * (float)M_PI * A_SECTIONS[i].hz;
        a_section_t *s = &a->section[i];
        s->a1 = (w - k) / (k + w);
        if (A_SECTIONS[i].highpass) {
            s->b0 = k / (k + w);
            s->b1 = -s->b0;
        } else {
            s->b0 = w / (k + w);
            s->b1 = s->b0;
        }
        s->x1 = s->y1 = 0;
        gain *= (s->b0 + s->b1 * z1) / (1.0f + s->a1 * z1);
    }
    a->scale = 1.0f / cabsf(gain);
}

static inline float a_weight(a_weight_t *a, float x)
{
    for (int i = 0; i < A_WEIGHT_SECTIONS; i++) {
        a_section_t *s = &a->section[i];
        float y = s->b0 * x + s->b1 * s->x1 - s->a1 * s->y1;
        s->x1 = x;
        s->y1 = y;
        x = y;
    }
    return x * a->scale;
}

// --- Level meter --------------------------------------------------------------------------

void level_meter_init(level_meter_t *m)
{
    a_weight_init(&m->a);
    level_meter_reset(m);
}

void level_meter_reset(level_meter_t *m)
{
    m->energy = 0;
    m->energy_a = 0;
    m->samples = 0;
    m->loudest_block = 0;
    m->loudest_block_a = 0;
}

void level_meter_add(level_meter_t *m, const float *x, size_t n)
{
    if (n == 0) {
        return;
    }
    // Sum the block in float (the ESP32 has a float unit, but double is done in software).
    float e = 0, ea = 0;
    for (size_t i = 0; i < n; i++) {
        float xa = a_weight(&m->a, x[i]);
        e += x[i] * x[i];
        ea += xa * xa;
    }
    m->energy += e;
    m->energy_a += ea;
    m->samples += n;
    if (e / n > m->loudest_block) {
        m->loudest_block = e / n;
    }
    if (ea / n > m->loudest_block_a) {
        m->loudest_block_a = ea / n;
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

float level_meter_level_dba(const level_meter_t *m)
{
    return m->samples ? mean_square_to_spl(m->energy_a / m->samples) : NAN;
}

float level_meter_peak_dba(const level_meter_t *m)
{
    return m->samples ? mean_square_to_spl(m->loudest_block_a) : NAN;
}
