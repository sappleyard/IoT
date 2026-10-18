#include "radio.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static radio_recv_cb_t s_on_recv;
static radio_sent_cb_t s_on_sent;

static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (s_on_recv) {
        s_on_recv(info->src_addr, data, len, info->rx_ctrl ? info->rx_ctrl->rssi : 0);
    }
}

// The send callback's signature changed in ESP-IDF 5.5.
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
static void on_sent(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    if (s_on_sent) {
        s_on_sent(info->des_addr, status == ESP_NOW_SEND_SUCCESS);
    }
}
#else
static void on_sent(const uint8_t *mac, esp_now_send_status_t status)
{
    if (s_on_sent) {
        s_on_sent(mac, status == ESP_NOW_SEND_SUCCESS);
    }
}
#endif

esp_err_t radio_init(radio_recv_cb_t recv_cb, radio_sent_cb_t sent_cb)
{
    s_on_recv = recv_cb;
    s_on_sent = sent_cb;

    esp_err_t err = nvs_flash_init();   // Wi-Fi keeps calibration data in NVS
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));   // station mode, never connects
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_RADIO_CHANNEL, WIFI_SECOND_CHAN_NONE));
#if CONFIG_RADIO_LONG_RANGE
    // Espressif long-range mode: slower, but reaches much further through walls.
    // Only boards also in LR mode can hear it, so set it on all three.
    ESP_ERROR_CHECK(esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_LR));
#endif

    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_recv));
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_sent));
    return ESP_OK;
}

esp_err_t radio_add_peer(const uint8_t mac[6])
{
    if (esp_now_is_peer_exist(mac)) {
        return ESP_OK;
    }
    esp_now_peer_info_t peer = {
        .channel = 0,            // 0 = whatever channel we're on
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, mac, ESP_NOW_ETH_ALEN);
    return esp_now_add_peer(&peer);
}

esp_err_t radio_send(const uint8_t mac[6], const void *data, size_t len)
{
    return esp_now_send(mac, data, len);
}

void radio_own_mac(uint8_t mac[6])
{
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
}

void radio_format_mac(const uint8_t mac[6], char out[RADIO_MAC_STR_LEN])
{
    snprintf(out, RADIO_MAC_STR_LEN, "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

bool radio_parse_mac(const char *str, uint8_t mac[6])
{
    unsigned int b[6];
    if (sscanf(str, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (b[i] > 0xFF) {
            return false;
        }
        mac[i] = (uint8_t)b[i];
    }
    return true;
}
