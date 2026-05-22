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
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#ifndef WIFI_SSID
#define WIFI_SSID               "MeshSense_TX_AP"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS               "mstx1234"
#endif
#ifndef COLLECTOR_IP
#define COLLECTOR_IP            "192.168.4.2"
#endif
#ifndef COLLECTOR_PORT
#define COLLECTOR_PORT          9999
#endif
#ifndef DEVICE_ID
#define DEVICE_ID               101
#endif

#define MAGIC_CS                0x4353
#define SCHEMA_VERSION          1
#define HEADER_LEN              40
#define PAYLOAD_TYPE_CSI_AMP    1
#define NOISE_FLOOR_UNKNOWN     (-128)

#define MAX_AMP_SAMPLES         64
#define CSI_BUFFER_MAX_BYTES    512
#define SEND_INTERVAL_US        9000   /* 9ms — 100Hz 상한 (jitter 허용) */
#define CSI_RAW_MAX_BYTES       256
#define CSI_QUEUE_LEN           32

typedef struct {
    uint16_t raw_len;
    uint8_t channel;
    int8_t rssi;
    int8_t raw[CSI_RAW_MAX_BYTES];
} csi_raw_item_t;

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

static const char *TAG = "CSI_SENDER";
static int g_udp_sock = -1;
static struct sockaddr_in g_collector_addr;
static uint32_t g_seq = 0;
static uint32_t g_runtime_device_id = 0;
static int64_t g_last_send_us = 0;
static QueueHandle_t g_csi_queue = NULL;
static volatile uint32_t g_csi_queue_drop = 0;
static volatile uint32_t g_csi_cb_count = 0;
static volatile uint32_t g_csi_throttle_drop = 0;
static volatile uint32_t g_csi_sent = 0;
static volatile uint32_t g_csi_filter_drop = 0;
static uint8_t g_ap_bssid[6] = {0};
static bool g_ap_bssid_set = false;

static float to_amplitude(const int8_t i, const int8_t q)
{
    return sqrtf((float)(i * i + q * q));
}

static size_t extract_amp_from_raw(const int8_t *raw, size_t raw_len, float *amp_out, size_t max_count)
{
    if (!raw || !amp_out || max_count == 0 || raw_len < 2) {
        return 0;
    }

    size_t complex_count = raw_len / 2;
    size_t out_count = complex_count < max_count ? complex_count : max_count;

    for (size_t i = 0; i < out_count; ++i) {
        int8_t i_part = raw[2 * i];
        int8_t q_part = raw[2 * i + 1];
        amp_out[i] = to_amplitude(i_part, q_part);
    }

    return out_count;
}

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

static void send_csi_from_raw(const csi_raw_item_t *item)
{
    if (!item || g_udp_sock < 0) {
        return;
    }

    int64_t now_us = esp_timer_get_time();
    if (g_last_send_us != 0 && (now_us - g_last_send_us) < SEND_INTERVAL_US) {
        g_csi_throttle_drop++;
        return;
    }
    g_last_send_us = now_us;
    g_csi_sent++;

    float amp_raw[MAX_AMP_SAMPLES];
    float amp_ma[MAX_AMP_SAMPLES];
    size_t count = extract_amp_from_raw(item->raw, item->raw_len, amp_raw, MAX_AMP_SAMPLES);
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
    hdr.session_id = 0;
    hdr.device_id = g_runtime_device_id;
    hdr.seq = g_seq++;
    hdr.timestamp_us = (uint64_t)now_us;
    hdr.channel = item->channel;
    hdr.rssi_dbm = item->rssi;
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

static void csi_worker_task(void *arg)
{
    (void)arg;
    csi_raw_item_t item;

    while (1) {
        if (xQueueReceive(g_csi_queue, &item, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        send_csi_from_raw(&item);
    }
}

static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;
    if (!info || !info->buf || info->len < 2 || g_csi_queue == NULL) {
        return;
    }

    g_csi_cb_count++;
    /* 우리 AP BSSID에서 온 frame만 통과 — 주변 다른 네트워크 CSI 차단 */
    if (g_ap_bssid_set) {
        const uint8_t *src = info->mac;
        if (memcmp(src, g_ap_bssid, 6) != 0) {
            g_csi_filter_drop++;
            return;
        }
    }
    csi_raw_item_t item = {0};
    item.raw_len = (uint16_t)(info->len > CSI_RAW_MAX_BYTES ? CSI_RAW_MAX_BYTES : info->len);
    memcpy(item.raw, info->buf, item.raw_len);
    item.channel = info->rx_ctrl.channel;
    item.rssi = info->rx_ctrl.rssi;

    if (xQueueSend(g_csi_queue, &item, 0) != pdTRUE) {
        g_csi_queue_drop++;
    }
}

static void init_csi_pipeline(void)
{
    g_csi_queue = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_raw_item_t));
    if (g_csi_queue == NULL) {
        ESP_LOGE(TAG, "CSI queue alloc failed");
        return;
    }

    xTaskCreate(csi_worker_task, "csi_worker", 4096, NULL, 5, NULL);

    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = true,
        .ltf_merge_en = true,
        .channel_filter_en = false, /* HT40 secondary 등 모든 frame에서 CSI */
        .manu_scale = false,
        .shift = false,
    };

    /* promiscuous 필수: STA 모드만으로는 ESP-NOW broadcast frame의 CSI 콜백이
     * 거의 발생하지 않음(실측 0.2Hz). promiscuous로 모든 frame을 CSI 경로로 통과시킴. */
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI enabled (queue=%d, worker offload)", CSI_QUEUE_LEN);
}

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

static void disable_wifi_power_save(void)
{
    esp_err_t err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Wi-Fi power save disabled (CSI target 100Hz)");
    } else {
        ESP_LOGW(TAG, "esp_wifi_set_ps(WIFI_PS_NONE) failed: %s", esp_err_to_name(err));
    }
}

static void wifi_sta_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
        wifi_event_sta_connected_t *ev = (wifi_event_sta_connected_t *)event_data;
        memcpy(g_ap_bssid, ev->bssid, 6);
        g_ap_bssid_set = true;
        ESP_LOGI(TAG, "AP BSSID locked for CSI filter: " MACSTR, MAC2STR(g_ap_bssid));
        disable_wifi_power_save();
    }
}

static void init_wifi_sta(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_sta_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, WIFI_SSID, sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, WIFI_PASS, sizeof(wifi_config.sta.password) - 1);
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.listen_interval = 1;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    /* STA도 HT20 강제 → AP/STA 양쪽 HT20으로 협상되어 secondary-channel 누락 제거 */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    disable_wifi_power_save();
    ESP_ERROR_CHECK(esp_wifi_connect());

    ESP_LOGI(TAG, "Wi-Fi STA start/connect requested");
}

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    g_runtime_device_id = (uint32_t)DEVICE_ID;
    ESP_LOGI(TAG, "device_id=%" PRIu32 " (run session_id on Mac)", g_runtime_device_id);
    init_wifi_sta();
    init_udp_sender();
    init_csi_pipeline();

    uint32_t prev_cb = 0, prev_sent = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        uint32_t cb = g_csi_cb_count;
        uint32_t sent = g_csi_sent;
        uint32_t throttle = g_csi_throttle_drop;
        uint32_t qdrop = g_csi_queue_drop;
        ESP_LOGI(TAG,
                 "5s: cb=%" PRIu32 " (+%" PRIu32 ", %.1fHz) sent=%" PRIu32 " (+%" PRIu32 ", %.1fHz) throttle_drop=%" PRIu32 " filter_drop=%" PRIu32 " qdrop=%" PRIu32,
                 cb, cb - prev_cb, (cb - prev_cb) / 5.0f,
                 sent, sent - prev_sent, (sent - prev_sent) / 5.0f,
                 throttle, g_csi_filter_drop, qdrop);
        prev_cb = cb;
        prev_sent = sent;
    }
}
