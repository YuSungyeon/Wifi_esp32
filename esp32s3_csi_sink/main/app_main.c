/*
 * MeshSense SINK — RX 들이 ESP-NOW 로 올려보낸 CSI 프레임을 USB 로 host 에 넘긴다.
 *
 * 실시간 경로에서 유일하게 host(맥북)에 USB 로 연결되는 보드다. RX 는 방 안에 흩어져
 * 전원만 있으면 되고, 맥북 Wi-Fi 로는 ESP-NOW 를 받을 수 없어(Espressif 독자 프로토콜)
 * "ESP-NOW 를 알아듣는 귀"가 하나 필요하다.
 *
 *   TX ──ESP-NOW 10ms 자극──▶ RX ──ESP-NOW 업링크──▶ SINK ──USB──▶ Mac
 *
 * 프레임을 **해석하지 않고 그대로 흘려보낸다.** RX 가 만든 v4 프레임(CRC 포함)이
 * host 까지 무손상으로 도달하는지 확인할 수 있고, 프레임 규격이 바뀌어도 sink 는
 * 고칠 게 없다. host 파서는 USB 직결과 동일하다 (scripts/csi_store.py).
 */

#include <string.h>
#include <inttypes.h>

#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "driver/usb_serial_jtag.h"

#define SINK_CHANNEL            11
#define SINK_RINGBUF_BYTES      (64 * 1024)
#define SINK_USJ_TX_BUF_BYTES   (16 * 1024)

/* 역할별 MAC — esp32s3_csi_recv_poc/main/app_main.c 와 짝이다.
 * RX 가 unicast 로 이 MAC 에 보내므로 여기서도 같은 값을 써야 한다. */
static const uint8_t SINK_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0xff};

static const char *TAG = "csi_sink";
static RingbufHandle_t g_ringbuf = NULL;
static volatile uint32_t g_recv = 0;      /* ESP-NOW 로 받은 프레임 */
static volatile uint32_t g_sent = 0;      /* USB 로 완전히 나간 프레임 */
static volatile uint32_t g_drop = 0;      /* ring buffer full */
static volatile uint32_t g_foreign = 0;   /* 우리 프레임이 아닌 ESP-NOW 패킷 (주로 TX 자극) */

/* sink 는 프레임을 해석하지 않는다. 서명·길이 확인에 필요한 앞부분만 안다. */
#define CSI_FRAME_VERSION 4
typedef struct { uint8_t magic[2]; uint8_t version; uint8_t frame_type;
                 uint16_t total_len; } csi_frame_min_t;

static void espnow_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    (void)info;
    /* 이 채널에는 RX 업링크만 오는 게 아니다 — TX 의 자극 broadcast(4바이트 카운터)도
     * 같이 들어온다. 그대로 흘려보내면 프레임 사이에 4바이트 쓰레기가 끼어 host 가
     * 매 프레임 재동기화한다 (실측: resync 가 프레임 수와 같았다).
     * 프레임 서명(magic + version)으로 우리 프레임만 통과시킨다. */
    if (len < (int)sizeof(csi_frame_min_t) || len > 512) {
        g_foreign++;
        return;
    }
    if (data[0] != 0x53 || data[1] != 0x43 || data[2] != CSI_FRAME_VERSION) {
        g_foreign++;
        return;
    }
    g_recv++;
    /* 콜백 안에서 USB 를 건드리지 않는다 — Wi-Fi task 가 막히면 수신이 무너진다.
     * (RX 쪽에서 CSI 콜백에 동기 I/O 를 넣었다가 ~50Hz 로 붕괴한 전례가 있다.) */
    if (xRingbufferSend(g_ringbuf, data, len, 0) != pdTRUE) {
        g_drop++;
    }
}

/* ring buffer → USB-CDC. 부분 write 를 반드시 이어서 보낸다. */
static void usb_writer_task(void *arg)
{
    (void)arg;
    while (1) {
        size_t len = 0;
        uint8_t *p = (uint8_t *)xRingbufferReceive(g_ringbuf, &len, portMAX_DELAY);
        if (!p) continue;
        /* ring buffer 는 NOSPLIT 항목을 4바이트 정렬 크기로 돌려준다 — 172바이트 프레임에
         * len=176 이 온다. 그대로 쓰면 프레임마다 4바이트 쓰레기가 붙어 host 가 매번
         * 재동기화한다. 프레임 앞 6바이트에 total_len 이 들어 있으니 그것을 쓴다
         * (sink 는 프레임을 해석하지 않지만 길이만은 봐야 한다). */
        if (len >= sizeof(csi_frame_min_t) && p[0] == 0x53 && p[1] == 0x43) {
            uint16_t declared = (uint16_t)(p[4] | (p[5] << 8));
            if (declared >= 8 && declared <= len) {
                len = declared;
            }
        }
        size_t off = 0;
        while (off < len) {
            int w = usb_serial_jtag_write_bytes(p + off, len - off, pdMS_TO_TICKS(100));
            if (w <= 0) break;          /* host 가 안 읽는 중 — 이 프레임은 포기 */
            off += (size_t)w;
        }
        vRingbufferReturnItem(g_ringbuf, p);
        if (off == len) g_sent++;
    }
}

static void hz_log_task(void *arg)
{
    (void)arg;
    uint32_t prev = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        uint32_t r = g_recv;
        /* 이 로그는 GPIO43 UART 로만 나간다 (console primary). USB 스트림은 오염되지 않는다. */
        ESP_LOGI(TAG, "5s: recv=%" PRIu32 " (+%" PRIu32 ", %.1fHz) usb=%" PRIu32
                       " drop=%" PRIu32 " foreign=%" PRIu32,
                 r, r - prev, (r - prev) / 5.0f, g_sent, g_drop, g_foreign);
        prev = r;
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(SINK_CHANNEL, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, SINK_MAC));

    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));

    usb_serial_jtag_driver_config_t usj = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usj.tx_buffer_size = SINK_USJ_TX_BUF_BYTES;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usj));

    g_ringbuf = xRingbufferCreate(SINK_RINGBUF_BYTES, RINGBUF_TYPE_NOSPLIT);
    ESP_ERROR_CHECK(g_ringbuf ? ESP_OK : ESP_ERR_NO_MEM);
    xTaskCreate(usb_writer_task, "usb_writer", 4096, NULL, 5, NULL);
    xTaskCreate(hz_log_task, "hz_log", 3072, NULL, 4, NULL);

    ESP_LOGI(TAG, "================ CSI SINK ================");
    ESP_LOGI(TAG, "channel=%d mac=" MACSTR, SINK_CHANNEL, MAC2STR(SINK_MAC));
}
