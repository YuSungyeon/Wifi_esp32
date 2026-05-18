#include <inttypes.h>
#include <stdio.h>
#include <string.h>

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
#define TX_AP_SSID "WiSLAR_TX_AP"
#endif
#ifndef TX_AP_PASS
#define TX_AP_PASS "wislartx123"
#endif
#ifndef TX_AP_CHANNEL
#define TX_AP_CHANNEL 6 // wifi 채널
#endif
#ifndef TX_AP_MAX_CONN
#define TX_AP_MAX_CONN 4 // 최대 연결 가능한 STA 수
#endif
#ifndef TX_AP_BROADCAST_PORT
#define TX_AP_BROADCAST_PORT 3333 // 브로드캐스트 송신포트
#endif
#ifndef TX_AP_INTERVAL_MS
#define TX_AP_INTERVAL_MS 10 // heartbeat 전송 간격(ms)
#endif
#ifndef TX_AP_PAYLOAD_BYTES
#define TX_AP_PAYLOAD_BYTES 64 // udp 페이로드 크기(헤더 제외)
#endif
#ifndef TX_AP_SESSION_ID
#define TX_AP_SESSION_ID 1 // 세션 식별자(로그 분석 시 활용)
#endif
#ifndef TX_AP_NODE_ID
#define TX_AP_NODE_ID 1 // 노드 식별자
#endif

#define TX_ADDR_MAGIC0 'T'
#define TX_ADDR_MAGIC1 'X'

/* esp_wifi_80211_tx()에 넘길 최소 802.11 data frame 헤더입니다.
 * addr2를 일반 MAC 대신 [T][X][seq32] 형식의 식별 필드로 사용합니다. */
typedef struct __attribute__((packed)) {
    uint16_t frame_ctrl;
    uint16_t duration;
    uint8_t addr1[6]; // receiver: broadcast로 두어 RX 여러 대가 같은 frame을 볼 수 있게 함
    uint8_t addr2[6]; // transmitter 필드를 [magic 2B][seq 4B]로 사용
    uint8_t addr3[6]; // BSSID: SoftAP 자신의 MAC
    uint16_t seq_ctrl;
} wifi_data_header_t;

static const char *TAG = "TX_AP_NODE";
static uint32_t g_seq = 0;
/* raw 802.11 header의 addr2/addr3에 넣기 위해 SoftAP MAC을 부팅 시 저장합니다. */
static uint8_t g_ap_mac[6] = {0};

/* AP에 접속/해제되는 STA 이벤트를 로그로 남겨
 * AP 상태를 현장에서 빠르게 확인할 수 있게 합니다. */
static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_data;
    if (event_base != WIFI_EVENT) {
        return;
    }

    if (event_id == WIFI_EVENT_AP_STACONNECTED) { // STA가 AP에 연결된 이벤트
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
    // Wi-Fi 초기화 및 SoftAP 설정
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap(); // SoftAP 인터페이스 생성, 192.168.4.1 기본 IP 설정

    // Wi-Fi 초기화 및 이벤트 핸들러 등록
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));

    // SoftAP 설정 구성
    wifi_config_t ap_cfg = {0};
    strncpy((char *)ap_cfg.ap.ssid, TX_AP_SSID, sizeof(ap_cfg.ap.ssid) - 1);
    strncpy((char *)ap_cfg.ap.password, TX_AP_PASS, sizeof(ap_cfg.ap.password) - 1);
    ap_cfg.ap.ssid_len = (uint8_t)strlen(TX_AP_SSID);
    ap_cfg.ap.channel = TX_AP_CHANNEL; // 고정 채널 설정
    ap_cfg.ap.max_connection = TX_AP_MAX_CONN; // 최대 연결 가능한 STA 수
    ap_cfg.ap.pmf_cfg.required = false;

    // WPA2-PSK 인증 모드 설정: 패스워드 길이가 8자 이상이면 WPA2-PSK, 그렇지 않으면 OPEN
    if (strlen(TX_AP_PASS) >= 8) {
        ap_cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        ap_cfg.ap.authmode = WIFI_AUTH_OPEN;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_AP, g_ap_mac)); // SoftAP MAC 주소 저장

    ESP_LOGI(TAG,
             "SoftAP started ssid=%s channel=%d max_conn=%d auth=%s mac=" MACSTR,
             TX_AP_SSID,
             TX_AP_CHANNEL,
             TX_AP_MAX_CONN,
             ap_cfg.ap.authmode == WIFI_AUTH_OPEN ? "OPEN" : "WPA2",
             MAC2STR(g_ap_mac));
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

static void fill_wifi_header(wifi_data_header_t *hdr, uint32_t seq)
{
    const uint8_t broadcast_mac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

    memset(hdr, 0, sizeof(*hdr));
    /* 0x0208은 802.11 data frame + FromDS 비트입니다.
     * AP가 STA/RX 방향으로 보내는 data frame 형태로 만들기 위한 최소 설정입니다. */
    hdr->frame_ctrl = 0x0208; /* data frame, FromDS=1: AP -> STA */
    memcpy(hdr->addr1, broadcast_mac, sizeof(hdr->addr1));
    /* addr2에 TX magic과 seq를 직접 숨깁니다.
     * addr2[0..1] = 'T','X', addr2[2..5] = seq little-endian */
    hdr->addr2[0] = TX_ADDR_MAGIC0;
    hdr->addr2[1] = TX_ADDR_MAGIC1;
    hdr->addr2[2] = (uint8_t)(seq & 0xFFu);
    hdr->addr2[3] = (uint8_t)((seq >> 8) & 0xFFu);
    hdr->addr2[4] = (uint8_t)((seq >> 16) & 0xFFu);
    hdr->addr2[5] = (uint8_t)((seq >> 24) & 0xFFu);
    memcpy(hdr->addr3, g_ap_mac, sizeof(hdr->addr3));
}

/* raw 802.11 data frame을 주기 전송합니다.
 * 결과 frame 구조:
 [802.11 data header(addr2=[TX][seq])][dummy payload] */
static void tx_raw_frame_task(void *arg)
{
    (void)arg;

    uint16_t payload_len = (TX_AP_PAYLOAD_BYTES < 8) ? 8 : TX_AP_PAYLOAD_BYTES;
    uint8_t tx_buf[512]; // 충분히 큰 버퍼를 선언하여 헤더+페이로드가 들어갈 수 있게 합니다.
    if (sizeof(tx_buf) < sizeof(wifi_data_header_t) + payload_len) {
        ESP_LOGE(TAG, "TX buffer too small");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG,
             "Starting raw 802.11 heartbeat: interval=%dms payload=%dB",
             TX_AP_INTERVAL_MS,
             payload_len);

    while (1) {
        uint32_t seq = g_seq++;
        wifi_data_header_t wifi_hdr = {0};
        fill_wifi_header(&wifi_hdr, seq);

        memcpy(tx_buf, &wifi_hdr, sizeof(wifi_hdr));
        fill_payload(tx_buf + sizeof(wifi_hdr), payload_len, seq);
        size_t packet_len = sizeof(wifi_hdr) + payload_len;

        /* en_sys_seq=true로 두어 Wi-Fi 드라이버가 802.11 sequence control 값을 관리하게 합니다.
         * 우리가 추적에 쓰는 번호는 addr2[2..5]에 넣은 seq입니다. */
        esp_err_t err = esp_wifi_80211_tx(WIFI_IF_AP, tx_buf, packet_len, true);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "esp_wifi_80211_tx failed: %s", esp_err_to_name(err));
        }

        if ((seq % 100) == 0) {
            ESP_LOGI(TAG, "tx raw frame seq=%" PRIu32 " bytes=%d", seq, (int)packet_len);
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
    xTaskCreate(tx_raw_frame_task, "tx_raw_frame_task", 4096, NULL, 5, NULL);
}
