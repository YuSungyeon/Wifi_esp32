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
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

/* =========================
 * 런타임 설정(기본값)
 * =========================
 * 아래 값은 기본값이며 CMake -D 옵션으로 쉽게 바꿀 수 있습니다.
 * (SSID/채널/주기/세션ID 등을 코드 수정 없이 운용 가능)
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
#ifndef TX_AP_PAYLOAD_BYTES
#define TX_AP_PAYLOAD_BYTES 64
#endif
#ifndef TX_AP_NODE_ID
#define TX_AP_NODE_ID 1
#endif

#define TX_PACKET_MAGIC 0x5458u /* "TX" */
#define TX_PACKET_VERSION 1u

/* 디버깅/추적용 heartbeat 헤더
 * - RX가 이 패킷을 직접 파싱하는 구조는 아닙니다.
 * - 목적은 RF 트래픽을 일정하게 발생시키고,
 *   TX/AP 노드 상태(seq 증가)를 관찰하기 위함입니다. */
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
static uint32_t g_seq = 0;

/* AP에 접속/해제되는 STA 이벤트를 로그로 남겨
 * AP 상태를 현장에서 빠르게 확인할 수 있게 합니다. */
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

/* RX 노드가 붙을 SoftAP를 올립니다.
 * CSI 재현성을 위해 채널을 고정해서 운용하는 것을 기본으로 합니다. */
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

    if (strlen(TX_AP_PASS) >= 8) {
        ap_cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        ap_cfg.ap.authmode = WIFI_AUTH_OPEN;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG,
             "SoftAP started ssid=%s channel=%d max_conn=%d auth=%s",
             TX_AP_SSID,
             TX_AP_CHANNEL,
             TX_AP_MAX_CONN,
             ap_cfg.ap.authmode == WIFI_AUTH_OPEN ? "OPEN" : "WPA2");
}

/* 더미 페이로드 생성:
 * 값 자체의 의미보다 "지속적인 무선 프레임 생성"이 목적입니다.
 * 즉, CSI 취득에 필요한 트래픽을 안정적으로 유지하기 위한 데이터입니다. */
static void fill_payload(uint8_t *payload, uint16_t payload_len, uint32_t seq)
{
    for (uint16_t i = 0; i < payload_len; ++i) {
        payload[i] = (uint8_t)((seq + i) & 0xFFu);
    }
}

/* AP 서브넷 브로드캐스트 주소로 heartbeat를 주기 전송합니다.
 * 외부 공유기 없이도 TX/AP 노드 단독으로 트래픽 소스를 만들 수 있습니다. */
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
             "Starting UDP broadcast: dst=192.168.4.255:%d interval=%dms payload=%dB",
             TX_AP_BROADCAST_PORT,
             TX_AP_INTERVAL_MS,
             payload_len);

    while (1) {
        /* session/seq를 포함한 헤더를 구성해
         * 나중에 로그 분석 시 세션 단위 추적이 가능하도록 합니다. */
        tx_heartbeat_header_t hdr = {0};
        hdr.magic = TX_PACKET_MAGIC;
        hdr.version = TX_PACKET_VERSION;
        hdr.session_id = 0; /* v1 reserved: run ID is Mac session_meta SSOT */
        hdr.tx_node_id = (uint32_t)TX_AP_NODE_ID;
        hdr.seq = g_seq++;
        hdr.timestamp_us = (uint64_t)esp_timer_get_time();
        hdr.payload_len = payload_len;

        memcpy(tx_buf, &hdr, sizeof(hdr));
        fill_payload(tx_buf + sizeof(hdr), payload_len, hdr.seq);
        size_t packet_len = sizeof(hdr) + payload_len;

        ssize_t sent = sendto(sock, tx_buf, packet_len, 0, (struct sockaddr *)&dst, sizeof(dst));
        if (sent < 0) {
            ESP_LOGW(TAG, "sendto failed");
        }

        if ((hdr.seq % 100) == 0) {
            ESP_LOGI(TAG, "tx heartbeat seq=%" PRIu32 " bytes=%d", hdr.seq, (int)packet_len);
        }

        vTaskDelay(pdMS_TO_TICKS(TX_AP_INTERVAL_MS));
    }
}

void app_main(void)
{
    /* TX/AP 전용 노드의 최소 부팅 경로:
     * NVS -> SoftAP -> heartbeat 송신 태스크 시작 */
    ESP_ERROR_CHECK(nvs_flash_init());
    init_softap();
    xTaskCreate(tx_broadcast_task, "tx_broadcast_task", 4096, NULL, 5, NULL);
}
