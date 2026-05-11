#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
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
 * - 아래 값은 "기본값"이고, 실제 운용에서는 CMake의 -D 옵션으로 덮어씁니다.
 * - 이렇게 하면 RX1/RX2/...를 코드 수정 없이 같은 소스로 운용할 수 있습니다.
 */
#ifndef WIFI_SSID
#define WIFI_SSID               "YOUR_WIFI_SSID"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS               "YOUR_WIFI_PASSWORD"
#endif
#ifndef COLLECTOR_IP
#define COLLECTOR_IP            "192.168.0.10"
#endif
#ifndef COLLECTOR_PORT
#define COLLECTOR_PORT          9999
#endif

#ifndef SESSION_ID
#define SESSION_ID              1
#endif
#ifndef DEVICE_ID
#define DEVICE_ID               101
#endif

#define MAGIC_CS                0x4353
#define SCHEMA_VERSION          1
#define HEADER_LEN              40
#define PAYLOAD_TYPE_CSI_AMP    1
#define NOISE_FLOOR_UNKNOWN     (-128)

#define MAX_AMP_SAMPLES         64  /* 20MHz OFDM FFT bins: 우선 64개 전송, 유효 톤 선별은 PC에서 수행 */
#define CSI_BUFFER_MAX_BYTES    512
#define SEND_INTERVAL_US        10000  /* 10ms */

/* UDP 전송과 장치 시퀀스 관리를 위한 전역 상태값 */
static const char *TAG = "CSI_SENDER";
static int g_udp_sock = -1;
static struct sockaddr_in g_collector_addr;
static uint32_t g_seq = 0;
static uint32_t g_runtime_device_id = 0;
static int64_t g_last_send_us = 0;

#pragma pack(push, 1)
typedef struct {
    uint16_t magic;
    uint8_t version;
    uint8_t header_len;
    uint8_t payload_type;
    uint8_t flags;
    uint16_t reserved0;
    uint32_t session_id;
    uint32_t device_id;
    uint32_t seq;
    uint64_t timestamp_us;
    uint8_t channel;
    int8_t rssi_dbm;
    int8_t noise_floor_dbm;
    uint8_t reserved1;
    uint16_t sample_count;
    uint16_t reserved2;
    uint32_t crc32;
} csi_udp_header_v1_t;
#pragma pack(pop)

/* CSI의 복소수 샘플(I/Q)을 진폭(amplitude)으로 변환합니다.
 * 참고: info->buf는 [I0, Q0, I1, Q1, ...] 형태로 저장됩니다. */
static float to_amplitude(const int8_t i, const int8_t q)
{
    return sqrtf((float)(i * i + q * q));
}

/* CSI 콜백 버퍼에서 진폭 벡터만 추출합니다.
 * 반환값은 실제 추출된 샘플 수이며, max_count를 넘지 않습니다. */
static size_t extract_amp_from_csi(const wifi_csi_info_t *info, float *amp_out, size_t max_count)
{
    if (!info || !amp_out || max_count == 0 || info->len < 2) {
        return 0;
    }

    size_t complex_count = info->len / 2;
    size_t out_count = complex_count < max_count ? complex_count : max_count;
    const int8_t *raw = info->buf;

    for (size_t i = 0; i < out_count; ++i) {
        int8_t i_part = raw[2 * i];
        int8_t q_part = raw[2 * i + 1];
        amp_out[i] = to_amplitude(i_part, q_part);
    }

    return out_count;
}

/* 3-tap 이동평균으로 프레임 간 흔들림(jitter)을 완화합니다. */
static size_t moving_average_3tap(const float *in, float *out, size_t n)
{
    if (!in || !out || n == 0) {
        return 0;
    }

    if (n == 1) {
        out[0] = in[0];
        return 1;
    }

    out[0] = (in[0] + in[1]) * 0.5f;
    for (size_t i = 1; i + 1 < n; ++i) {
        out[i] = (in[i - 1] + in[i] + in[i + 1]) / 3.0f;
    }
    out[n - 1] = (in[n - 2] + in[n - 1]) * 0.5f;
    return n;
}

/* 프레임 단위 z-score 정규화:
 * 수신 이득 변화(AGC 등)로 인한 스케일 드리프트를 줄여줍니다. */
static void zscore_inplace(float *x, size_t n)
{
    if (!x || n == 0) {
        return;
    }

    float mean = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        mean += x[i];
    }
    mean /= (float)n;

    float var = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        float d = x[i] - mean;
        var += d * d;
    }
    var /= (float)n;

    float std = sqrtf(var + 1e-6f);
    for (size_t i = 0; i < n; ++i) {
        x[i] = (x[i] - mean) / std;
    }
}

/* 이상치 클리핑:
 * 극단값을 잘라 후단 임계값 로직/전송 안정성을 높입니다. */
static void clip_outlier_inplace(float *x, size_t n, float lo, float hi)
{
    if (!x) {
        return;
    }
    for (size_t i = 0; i < n; ++i) {
        if (x[i] < lo) {
            x[i] = lo;
        } else if (x[i] > hi) {
            x[i] = hi;
        }
    }
}

/* CSI 콜백이 들어올 때마다 UDP 패킷 1개를 만들어 전송합니다.
 * 처리 순서:
 * 1) raw CSI(I/Q) -> amplitude 변환
 * 2) 이동평균 + 정규화 + 클리핑
 * 3) 헤더 + float payload 패킹
 * 4) Mac 수집기로 sendto() 전송 */
static void send_csi_packet(const wifi_csi_info_t *info)
{
    if (!info || g_udp_sock < 0) {
        return;
    }

    int64_t now_us = esp_timer_get_time();
    if (g_last_send_us != 0 && (now_us - g_last_send_us) < SEND_INTERVAL_US) {
        return;
    }
    g_last_send_us = now_us;

    float amp_raw[MAX_AMP_SAMPLES];
    float amp_ma[MAX_AMP_SAMPLES];
    size_t count = extract_amp_from_csi(info, amp_raw, MAX_AMP_SAMPLES);
    if (count == 0) {
        return;
    }

    moving_average_3tap(amp_raw, amp_ma, count);
    zscore_inplace(amp_ma, count);
    clip_outlier_inplace(amp_ma, count, -3.0f, 3.0f);

    uint8_t buffer[CSI_BUFFER_MAX_BYTES];
    size_t payload_bytes = count * sizeof(float);
    size_t total_len = sizeof(csi_udp_header_v1_t) + payload_bytes;
    if (total_len > sizeof(buffer)) {
        return;
    }

    csi_udp_header_v1_t hdr = {0};
    hdr.magic = MAGIC_CS;
    hdr.version = SCHEMA_VERSION;
    hdr.header_len = HEADER_LEN;
    hdr.payload_type = PAYLOAD_TYPE_CSI_AMP;
    hdr.flags = 0;
    hdr.session_id = SESSION_ID;
    hdr.device_id = g_runtime_device_id;
    hdr.seq = g_seq++;
    hdr.timestamp_us = (uint64_t)now_us;
    hdr.channel = info->rx_ctrl.channel;
    hdr.rssi_dbm = info->rx_ctrl.rssi;
    hdr.noise_floor_dbm = NOISE_FLOOR_UNKNOWN;
    hdr.sample_count = (uint16_t)count;
    hdr.crc32 = 0;

    memcpy(buffer, &hdr, sizeof(hdr));
    memcpy(buffer + sizeof(hdr), amp_ma, payload_bytes);

    sendto(
        g_udp_sock,
        buffer,
        total_len,
        0,
        (struct sockaddr *)&g_collector_addr,
        sizeof(g_collector_addr)
    );
}

/* 장치 ID 정책:
 * - DEVICE_ID > 0: 고정 ID 사용(현장 운영 권장)
 * - DEVICE_ID == 0: STA MAC의 뒤 3바이트로 자동 ID 생성 */
static uint32_t resolve_device_id(void)
{
    if (DEVICE_ID > 0) {
        return (uint32_t)DEVICE_ID;
    }

    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    uint32_t auto_id = ((uint32_t)mac[3] << 16) | ((uint32_t)mac[4] << 8) | (uint32_t)mac[5];
    ESP_LOGI(TAG, "Auto device_id from MAC: %" PRIu32, auto_id);
    return auto_id;
}

/* Wi-Fi CSI 콜백 진입점(핫패스) */
static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;
    send_csi_packet(info);
}

/* UDP 목적지(Mac 수집기) 소켓/주소 초기화 */
static void init_udp_sender(void)
{
    g_udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (g_udp_sock < 0) {
        ESP_LOGE(TAG, "Failed to create UDP socket");
        return;
    }

    memset(&g_collector_addr, 0, sizeof(g_collector_addr));
    g_collector_addr.sin_family = AF_INET;
    g_collector_addr.sin_port = htons(COLLECTOR_PORT);
    g_collector_addr.sin_addr.s_addr = inet_addr(COLLECTOR_IP);

    ESP_LOGI(TAG, "UDP target: %s:%d", COLLECTOR_IP, COLLECTOR_PORT);
}

/* 인프라 Wi-Fi에 STA로 접속:
 * RX 노드가 Mac 수집기(IP)까지 도달 가능하도록 네트워크를 엽니다. */
static void init_wifi_sta(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, WIFI_SSID, sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, WIFI_PASS, sizeof(wifi_config.sta.password) - 1);
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_connect());

    ESP_LOGI(TAG, "Wi-Fi STA start/connect requested");
}

/* CSI 수집 활성화 및 콜백 등록 */
static void init_csi(void)
{
    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = true,
        .ltf_merge_en = true,
        .channel_filter_en = true,
        .manu_scale = false,
        .shift = false,
    };

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI enabled");
}

void app_main(void)
{
    /* 부팅 순서가 중요합니다.
     * 1) NVS 초기화
     * 2) 장치 ID 확정
     * 3) 네트워크 경로 확보(STA + UDP)
     * 4) CSI 캡처 시작 */
    ESP_ERROR_CHECK(nvs_flash_init());
    g_runtime_device_id = resolve_device_id();
    ESP_LOGI(TAG, "session_id=%d device_id=%" PRIu32, SESSION_ID, g_runtime_device_id);
    init_wifi_sta();
    init_udp_sender();
    init_csi();

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
