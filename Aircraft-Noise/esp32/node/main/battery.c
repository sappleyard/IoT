#include "battery.h"

#include "sdkconfig.h"

#if CONFIG_NODE_BATTERY_ADC

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;   // NULL if this chip has no calibration data

void battery_init(void)
{
    adc_oneshot_unit_init_cfg_t unit = {.unit_id = ADC_UNIT_1};
    if (adc_oneshot_new_unit(&unit, &s_adc) != ESP_OK) {
        s_adc = NULL;
        return;
    }
    adc_oneshot_chan_cfg_t chan = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    adc_oneshot_config_channel(s_adc, CONFIG_NODE_BATTERY_ADC_CHANNEL, &chan);

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali = {
        .unit_id = ADC_UNIT_1,
        .chan = CONFIG_NODE_BATTERY_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali, &s_cali) != ESP_OK) {
        s_cali = NULL;
    }
#elif ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    adc_cali_line_fitting_config_t cali = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_line_fitting(&cali, &s_cali) != ESP_OK) {
        s_cali = NULL;
    }
#endif
}

uint16_t battery_read_mv(void)
{
    int raw = 0, mv = 0;
    if (!s_adc || adc_oneshot_read(s_adc, CONFIG_NODE_BATTERY_ADC_CHANNEL, &raw) != ESP_OK) {
        return 0;
    }
    if (!s_cali || adc_cali_raw_to_voltage(s_cali, raw, &mv) != ESP_OK) {
        mv = raw * 3100 / 4095;   // rough, uncalibrated
    }
    return (uint16_t)(mv * CONFIG_NODE_BATTERY_DIVIDER);
}

#else  // battery measuring off

void battery_init(void)
{
}

uint16_t battery_read_mv(void)
{
    return 0;
}

#endif
