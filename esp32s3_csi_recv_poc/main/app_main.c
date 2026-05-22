/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Get Start Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>

#include "nvs_flash.h"

#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "driver/usb_serial_jtag.h"

/* PoC: CSI 콜백 호출 카운터 (sender MAC 필터 통과 후 +1). 5초 태스크에서 Hz로 출력. */
static volatile uint32_t g_csi_recv_count = 0;
static volatile uint32_t g_uart_send_count = 0;
static volatile uint32_t g_ringbuf_drop = 0;

/* === Phase 2/3: 바이너리 CSI 프레임 UART 스트리밍 === */
#define CSI_FRAME_MAGIC          0x4353  /* 'CS' */
#define CSI_FRAME_VERSION        2       /* v2: tx_seq 필드 추가 */
#define CSI_MAX_RAW_BYTES        384     /* CSI raw bytes 안전 상한 (HT40 LTF ~ 384B 이하) */
#define CSI_RINGBUF_BYTES        (64 * 1024)  /* 64KB: 100Hz × ~320B × 2초 안전마진 */
#define CSI_USJ_TX_BUF_BYTES     (16 * 1024)  /* USB-Serial-JTAG 드라이버 TX 버퍼 */
#define CSI_TX_SEQ_OFFSET        15      /* ESP-NOW payload 내 uint32_t TX 카운터 위치 */

#pragma pack(push, 1)
typedef struct {
    uint16_t magic;          /* 0x4353 */
    uint8_t  version;        /* 2 */
    uint8_t  reserved0;
    uint16_t total_len;      /* 헤더 + raw 합산 길이 */
    uint16_t raw_len;        /* raw[] 바이트 수 */
    uint32_t seq;            /* RX 부팅부터 단조 증가 (보드별 독립) */
    uint64_t timestamp_us;   /* RX esp_timer_get_time() (보드별 독립) */
    int8_t   rssi;
    uint8_t  channel;
    int8_t   noise_floor;
    uint8_t  rate;
    uint16_t sig_len;
    uint16_t reserved1;
    uint32_t tx_seq;         /* TX 송신 카운터 (모든 RX 공통 — cross-RX 동기화 키) */
    /* raw[raw_len] tail */
} csi_frame_header_t;
#pragma pack(pop)
_Static_assert(sizeof(csi_frame_header_t) == 32, "csi_frame_header_t must be 32 bytes");

static RingbufHandle_t g_csi_ringbuf = NULL;
static uint32_t g_frame_seq = 0;

#define CONFIG_LESS_INTERFERENCE_CHANNEL   11
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE               WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                   0

/* PoC: CSV 출력은 921600 baud로도 50Hz가 한계라 콜백을 막는다.
 * 진짜 cb 속도 측정에는 0(off), 데이터 검증에는 1로. */
#define POC_DUMP_CSV 0

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
#define CSI_FORCE_LLTF                      0
#endif

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define CONFIG_GAIN_CONTROL                 1
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const char *TAG = "csi_recv";

static void wifi_init()
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#if CONFIG_IDF_TARGET_ESP32C5
    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20)
            || (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY && CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW_HT20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    if (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#else
    if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#endif

    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,//  WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

}

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        ESP_LOGW(TAG, "<%s> wifi_csi_cb", esp_err_to_name(ESP_ERR_INVALID_ARG));
        return;
    }

    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6)) {
        return;
    }
    /* PoC Hz 측정용 카운터 */
    g_csi_recv_count++;

    /* Phase 2: 바이너리 프레임을 ring buffer에 push. UART writer task가 drain. */
    if (g_csi_ringbuf) {
        size_t raw_len = info->len;
        if (raw_len > CSI_MAX_RAW_BYTES) raw_len = CSI_MAX_RAW_BYTES;
        size_t total = sizeof(csi_frame_header_t) + raw_len;

        /* TX 송신 카운터: ESP-NOW payload[15..18]. payload_len 안전 확인. */
        uint32_t tx_seq = 0;
        if (info->payload && info->payload_len >= CSI_TX_SEQ_OFFSET + 4) {
            memcpy(&tx_seq, info->payload + CSI_TX_SEQ_OFFSET, 4);
        }

        csi_frame_header_t hdr;
        hdr.magic        = CSI_FRAME_MAGIC;
        hdr.version      = CSI_FRAME_VERSION;
        hdr.reserved0    = 0;
        hdr.total_len    = (uint16_t)total;
        hdr.raw_len      = (uint16_t)raw_len;
        hdr.seq          = g_frame_seq++;
        hdr.timestamp_us = (uint64_t)esp_timer_get_time();
        hdr.rssi         = info->rx_ctrl.rssi;
        hdr.channel      = info->rx_ctrl.channel;
        hdr.noise_floor  = info->rx_ctrl.noise_floor;
        hdr.rate         = (uint8_t)info->rx_ctrl.rate;
        hdr.sig_len      = (uint16_t)info->rx_ctrl.sig_len;
        hdr.reserved1    = 0;
        hdr.tx_seq       = tx_seq;

        /* 두 번 push: header → raw. ringbuf NoSplit 모드에서 안전 — 단,
         * 한 번에 묶어 push하려면 stack 버퍼 사용 (HT40에서 최대 ~400B). */
        uint8_t buf[sizeof(csi_frame_header_t) + CSI_MAX_RAW_BYTES];
        memcpy(buf, &hdr, sizeof(hdr));
        memcpy(buf + sizeof(hdr), info->buf, raw_len);
        BaseType_t ok = xRingbufferSend(g_csi_ringbuf, buf, total, 0);
        if (ok != pdTRUE) {
            g_ringbuf_drop++;
        }
    }

#if POC_DUMP_CSV
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    static int s_count = 0;
    float compensate_gain = 1.0f;
    static uint8_t agc_gain = 0;
    static int8_t fft_gain = 0;
#if CONFIG_GAIN_CONTROL
    static uint8_t agc_gain_baseline = 0;
    static int8_t fft_gain_baseline = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);
    if (s_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_count == 100) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_gain_baseline, &fft_gain_baseline);
#if CONFIG_FORCE_GAIN
        esp_csi_gain_ctrl_set_rx_force_gain(agc_gain_baseline, fft_gain_baseline);
        ESP_LOGD(TAG, "fft_force %d, agc_force %d", fft_gain_baseline, agc_gain_baseline);
#endif
    }
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
    ESP_LOGI(TAG, "compensate_gain %f, agc_gain %d, fft_gain %d", compensate_gain, agc_gain, fft_gain);
#endif

    uint32_t rx_id = *(uint32_t *)(info->payload + 15);
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
    if (!s_count) {
        ESP_LOGI(TAG, "================ CSI RECV ================");
        ets_printf("type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,data\n");
    }

    ets_printf("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d",
               rx_id, MAC2STR(info->mac), rx_ctrl->rssi, rx_ctrl->rate,
               rx_ctrl->noise_floor, fft_gain, agc_gain,  rx_ctrl->channel,
               rx_ctrl->timestamp, rx_ctrl->sig_len, rx_ctrl->cur_bb_format);
#else
    if (!s_count) {
        ESP_LOGI(TAG, "================ CSI RECV ================");
        ets_printf("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data\n");
    }

    ets_printf("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
               rx_id, MAC2STR(info->mac), rx_ctrl->rssi, rx_ctrl->rate, rx_ctrl->sig_mode,
               rx_ctrl->mcs, rx_ctrl->cwb, rx_ctrl->smoothing, rx_ctrl->not_sounding,
               rx_ctrl->aggregation, rx_ctrl->stbc, rx_ctrl->fec_coding, rx_ctrl->sgi,
               rx_ctrl->noise_floor, rx_ctrl->ampdu_cnt, rx_ctrl->channel, rx_ctrl->secondary_channel,
               rx_ctrl->timestamp, rx_ctrl->ant, rx_ctrl->sig_len, rx_ctrl->sig_mode);

#endif
#if (CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61) && CSI_FORCE_LLTF
    int16_t csi = ((int16_t)(((((uint16_t)info->buf[1]) << 8) | info->buf[0]) << 4) >> 4);
    ets_printf(",%d,%d,\"[%d", (info->len - 2) / 2, info->first_word_invalid, (int16_t)(compensate_gain * csi));
    for (int i = 2; i < (info->len - 2); i += 2) {
        csi = ((int16_t)(((((uint16_t)info->buf[i + 1]) << 8) | info->buf[i]) << 4) >> 4);
        ets_printf(",%d", (int16_t)(compensate_gain * csi));
    }
#else
    ets_printf(",%d,%d,\"[%d", info->len, info->first_word_invalid, (int16_t)(compensate_gain * info->buf[0]));
    for (int i = 1; i < info->len; i++) {
        ets_printf(",%d", (int16_t)(compensate_gain * info->buf[i]));
    }
#endif
    ets_printf("]\"\n");
    s_count++;
#endif /* POC_DUMP_CSV */
}

/* PoC: 5초마다 누적 카운트와 직전 5초 Hz를 로그. */
static void hz_log_task(void *arg)
{
    (void)arg;
    uint32_t prev_cb = 0, prev_uart = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        uint32_t cb = g_csi_recv_count;
        uint32_t up = g_uart_send_count;
        uint32_t drop = g_ringbuf_drop;
        /* ESP_LOG는 UART0로 가지만 한 줄(<150B)이라 cb 100Hz 흐름에 영향 거의 없음 */
        ESP_LOGI(TAG, "5s: cb=%" PRIu32 " (+%" PRIu32 ", %.1fHz) uart=%" PRIu32
                       " (+%" PRIu32 ", %.1fHz) ringbuf_drop=%" PRIu32,
                 cb, cb - prev_cb, (cb - prev_cb) / 5.0f,
                 up, up - prev_uart, (up - prev_uart) / 5.0f,
                 drop);
        prev_cb = cb;
        prev_uart = up;
    }
}

/* USB-Serial-JTAG writer task: ring buffer에서 꺼내 USB-CDC로 그대로 쓴다.
 * ESP32-S3 dev 보드의 USB-C는 UART0가 아니라 USB-Serial-JTAG에 연결되어 있다. */
static void uart_writer_task(void *arg)
{
    (void)arg;
    while (1) {
        size_t len = 0;
        uint8_t *p = (uint8_t *)xRingbufferReceive(g_csi_ringbuf, &len, portMAX_DELAY);
        if (!p) continue;
        int written = usb_serial_jtag_write_bytes(p, len, pdMS_TO_TICKS(100));
        vRingbufferReturnItem(g_csi_ringbuf, p);
        if (written > 0) {
            g_uart_send_count++;
        }
    }
}

static void wifi_csi_init()
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /**< default config */
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config_t csi_config = {
        .enable                   = true,
        .acquire_csi_legacy       = false,
        .acquire_csi_force_lltf   = CSI_FORCE_LLTF,
        .acquire_csi_ht20         = true,
        .acquire_csi_ht40         = true,
        .acquire_csi_vht          = false,
        .acquire_csi_su           = false,
        .acquire_csi_mu           = false,
        .acquire_csi_dcm          = false,
        .acquire_csi_beamformed   = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg            = 0,
        .dump_ack_en              = false,
        .reserved                 = false
    };
#elif CONFIG_IDF_TARGET_ESP32C6
    wifi_csi_config_t csi_config = {
        .enable                 = true,
        .acquire_csi_legacy     = false,
        .acquire_csi_ht20       = true,
        .acquire_csi_ht40       = true,
        .acquire_csi_su         = true,
        .acquire_csi_mu         = true,
        .acquire_csi_dcm        = true,
        .acquire_csi_beamformed = true,
        .acquire_csi_he_stbc    = 2,
        .val_scale_cfg          = false,
        .dump_ack_en            = false,
        .reserved               = false
    };
#else
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
#endif
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

void app_main()
{
    /**
     * @brief Initialize NVS
     */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /**
     * @brief Initialize Wi-Fi
     */
    wifi_init();

    /**
     * @brief Initialize ESP-NOW
     *        ESP-NOW protocol see: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_now.html
     */

    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };

    wifi_esp_now_init(peer);

    /* Phase 2: USB-Serial-JTAG 드라이버 설치. ESP32-S3 dev 보드 USB-C가 여기로 연결됨. */
    usb_serial_jtag_driver_config_t usj_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usj_cfg.tx_buffer_size = CSI_USJ_TX_BUF_BYTES;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usj_cfg));

    /* 바이너리 CSI 프레임 스트리밍용 ring buffer + UART writer */
    g_csi_ringbuf = xRingbufferCreate(CSI_RINGBUF_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!g_csi_ringbuf) {
        ESP_LOGE(TAG, "ring buffer alloc failed");
    } else {
        xTaskCreate(uart_writer_task, "uart_writer", 4096, NULL, 5, NULL);
    }

    wifi_csi_init();

    /* PoC: 5초마다 Hz 출력 */
    xTaskCreate(hz_log_task, "hz_log", 3072, NULL, 4, NULL);
}
