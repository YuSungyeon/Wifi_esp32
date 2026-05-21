#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

/* =========================
 * 런타임 설정(기본값)
 * =========================
 */
#ifndef TX_AP_SSID
#define TX_AP_SSID "MeshSense_TX_AP"
#endif
#ifndef TX_AP_PASS
#define TX_AP_PASS "mstx1234"
#endif
#ifndef TX_AP_CHANNEL
#define TX_AP_CHANNEL 6
#endif
#ifndef TX_AP_MAX_CONN
#define TX_AP_MAX_CONN 4
#endif
#ifndef TX_AP_BROADCAST_PORT
#define TX_AP_BROADCAST_PORT 3333
#endif
#ifndef TX_AP_INTERVAL_MS
#define TX_AP_INTERVAL_MS 10
#endif
#ifndef TX_AP_BEACON_INTERVAL_TU
#define TX_AP_BEACON_INTERVAL_TU 100
#endif
#ifndef TX_AP_ESPNOW_INTERVAL_MS
#define TX_AP_ESPNOW_INTERVAL_MS 10
#endif
#ifndef TX_AP_PAYLOAD_BYTES
#define TX_AP_PAYLOAD_BYTES 64
#endif
#ifndef TX_AP_NODE_ID
#define TX_AP_NODE_ID 1
#endif

#define TX_PACKET_MAGIC 0x5458u /* "TX" */
#define TX_PACKET_VERSION 1u

static const uint8_t BROADCAST_MAC[ESP_NOW_ETH_ALEN] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t version;
    uint8_t reserved0;
    uint32_t session_id;
    uint32_t tx_node_id;
    uint32_t seq;
    uint64_t timestamp_us;
    uint16_t payload_len;
    uint16_t reserved1;
} tx_heartbeat_header_t;

static const char *TAG = "TX_AP_NODE";
static uint32_t g_udp_seq = 0;
static uint32_t g_enow_seq = 0;

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_data;
    if (event_base != WIFI_EVENT) {
        return;
    }

    if (event_id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *event = (wifi_event_ap_staconnected_t *)event_data;
        ESP_LOGI(TAG, "STA connected: " MACSTR ", aid=%d", MAC2STR(event->mac), event->aid);
    } else if (event_id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *event = (wifi_event_ap_stadisconnected_t *)event_data;
        ESP_LOGI(TAG, "STA disconnected: " MACSTR ", aid=%d", MAC2STR(event->mac), event->aid);
    }
}

static void init_esp_now(void)
{
    ESP_ERROR_CHECK(esp_now_init());

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, BROADCAST_MAC, ESP_NOW_ETH_ALEN);
    peer.channel = (uint8_t)TX_AP_CHANNEL;
    peer.ifidx = WIFI_IF_AP;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    ESP_LOGI(TAG,
             "ESP-NOW broadcaster ready ch=%d interval=%dms (CSI excitation, 100Hz target)",
             TX_AP_CHANNEL,
             TX_AP_ESPNOW_INTERVAL_MS);
}

static void init_softap(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));

    wifi_config_t ap_cfg = {0};
    strncpy((char *)ap_cfg.ap.ssid, TX_AP_SSID, sizeof(ap_cfg.ap.ssid) - 1);
    strncpy((char *)ap_cfg.ap.password, TX_AP_PASS, sizeof(ap_cfg.ap.password) - 1);
    ap_cfg.ap.ssid_len = (uint8_t)strlen(TX_AP_SSID);
    ap_cfg.ap.channel = TX_AP_CHANNEL;
    ap_cfg.ap.max_connection = TX_AP_MAX_CONN;
    ap_cfg.ap.pmf_cfg.required = false;
    /* 기본 100 TU(~102ms). 10 TU는 에어타임 붕괴로 CSI gap 유발 — ESP-NOW로 100Hz 유도 */
    ap_cfg.ap.beacon_interval = (uint16_t)TX_AP_BEACON_INTERVAL_TU;

    if (strlen(TX_AP_PASS) >= 8) {
        ap_cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        ap_cfg.ap.authmode = WIFI_AUTH_OPEN;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG,
             "SoftAP started ssid=%s channel=%d max_conn=%d beacon=%dTU auth=%s",
             TX_AP_SSID,
             TX_AP_CHANNEL,
             TX_AP_MAX_CONN,
             (int)ap_cfg.ap.beacon_interval,
             ap_cfg.ap.authmode == WIFI_AUTH_OPEN ? "OPEN" : "WPA2");
}

static void fill_payload(uint8_t *payload, uint16_t payload_len, uint32_t seq)
{
    for (uint16_t i = 0; i < payload_len; ++i) {
        payload[i] = (uint8_t)((seq + i) & 0xFFu);
    }
}

/* ESP-NOW 브로드캐스트: RX CSI 콜백을 100Hz에 가깝게 유도 (L3 UDP보다 L2에 가까움) */
static void esp_now_tx_task(void *arg)
{
    (void)arg;
    uint32_t fail_streak = 0;

    while (1) {
        uint32_t payload = g_enow_seq++;
        esp_err_t err = esp_now_send(BROADCAST_MAC, (const uint8_t *)&payload, sizeof(payload));
        if (err != ESP_OK) {
            if (++fail_streak == 1 || (fail_streak % 100) == 0) {
                ESP_LOGW(TAG, "esp_now_send: %s (streak=%" PRIu32 ")", esp_err_to_name(err), fail_streak);
            }
        } else {
            fail_streak = 0;
        }

        if ((payload % 100) == 0) {
            ESP_LOGI(TAG, "esp_now seq=%" PRIu32, payload);
        }

        vTaskDelay(pdMS_TO_TICKS(TX_AP_ESPNOW_INTERVAL_MS));
    }
}

/* 보조 L3 heartbeat (에어타임 점유 최소화를 위해 ESP-NOW보다 느리게 설정 가능) */
static void tx_broadcast_task(void *arg)
{
    (void)arg;

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "Failed to create UDP socket");
        vTaskDelete(NULL);
        return;
    }

    int broadcast_enable = 1;
    setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &broadcast_enable, sizeof(broadcast_enable));

    struct sockaddr_in dst = {0};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(TX_AP_BROADCAST_PORT);
    dst.sin_addr.s_addr = inet_addr("192.168.4.255");

    uint16_t payload_len = (TX_AP_PAYLOAD_BYTES < 8) ? 8 : TX_AP_PAYLOAD_BYTES;
    uint8_t tx_buf[512];
    if (sizeof(tx_buf) < sizeof(tx_heartbeat_header_t) + payload_len) {
        ESP_LOGE(TAG, "TX buffer too small");
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG,
             "UDP broadcast: :%d every %dms payload=%dB",
             TX_AP_BROADCAST_PORT,
             TX_AP_INTERVAL_MS,
             payload_len);

    while (1) {
        tx_heartbeat_header_t hdr = {0};
        hdr.magic = TX_PACKET_MAGIC;
        hdr.version = TX_PACKET_VERSION;
        hdr.session_id = 0;
        hdr.tx_node_id = (uint32_t)TX_AP_NODE_ID;
        hdr.seq = g_udp_seq++;
        hdr.timestamp_us = (uint64_t)esp_timer_get_time();
        hdr.payload_len = payload_len;

        memcpy(tx_buf, &hdr, sizeof(hdr));
        fill_payload(tx_buf + sizeof(hdr), payload_len, hdr.seq);
        size_t packet_len = sizeof(hdr) + payload_len;

        ssize_t sent = sendto(sock, tx_buf, packet_len, 0, (struct sockaddr *)&dst, sizeof(dst));
        if (sent < 0) {
            ESP_LOGW(TAG, "sendto failed");
        }

        vTaskDelay(pdMS_TO_TICKS(TX_AP_INTERVAL_MS));
    }
}

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    init_softap();
    init_esp_now();
    xTaskCreate(esp_now_tx_task, "esp_now_tx", 4096, NULL, 6, NULL);
    xTaskCreate(tx_broadcast_task, "tx_udp", 4096, NULL, 4, NULL);
}
