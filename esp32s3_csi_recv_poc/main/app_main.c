/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* MeshSense RX — esp-csi csi_recv 예제 기반.
 *
 * CSI 콜백 → ring buffer → USB-Serial-JTAG 바이너리 프레임(v3) 스트리밍.
 * 호스트 파서는 scripts/csi_store.py (프레임 규격 SSOT는 doc/pipeline/usb-collection.md).
 *
 * 이 파일의 불변식 3가지:
 *  1. CSI 콜백 안에서 동기 I/O 금지 — ets_printf 계열을 넣으면 WiFi driver task가
 *     백프레셔로 막혀 즉시 ~50Hz로 붕괴한다 (doc/overview/csi-rate-troubleshooting.md 결론부).
 *  2. 진폭 계산·정규화를 보드에서 하지 않는다 — raw I/Q를 그대로 보내고 호스트가 처리한다.
 *     (AP 파이프라인이 온디바이스 z-score로 시간축 진폭 변동을 지워버린 전례가 있다.)
 *  3. HT20 + htltf_en=false 로 LLTF 64 SC(raw 128B) 고정.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>
#include <stddef.h>

#include "nvs_flash.h"

#include "esp_mac.h"
#include "esp_random.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "driver/usb_serial_jtag.h"

/* 진단 카운터 (5초 태스크에서 Hz로 출력) */
static volatile uint32_t g_csi_recv_count = 0;   /* MAC 필터 통과한 CSI 콜백 수 */
static volatile uint32_t g_uart_send_count = 0;  /* USB로 완전히 나간 프레임 수 */
static volatile uint32_t g_ringbuf_drop = 0;     /* ring buffer full 로 버린 프레임 */
static volatile uint32_t g_uart_partial = 0;     /* 부분 write 후 재전송한 횟수 */

/* === 바이너리 CSI 프레임 스트리밍 (v3) === */
#define CSI_FRAME_MAGIC          0x4353  /* 'CS' */
#define CSI_FRAME_VERSION        4       /* v4: gain_comp(f32) 추가, 헤더 44B */
#define CSI_FRAME_TYPE_CSI       0
#define CSI_FRAME_TYPE_IDENT     1
#define CSI_MAX_RAW_BYTES        384     /* raw CSI 안전 상한 (HT40 LTF ~384B 이하) */
#define CSI_RINGBUF_BYTES        (64 * 1024)  /* 64KB: 100Hz × ~320B × 2초 안전마진 */
#define CSI_USJ_TX_BUF_BYTES     (16 * 1024)  /* USB-Serial-JTAG 드라이버 TX 버퍼 */

/* ESP-NOW payload 내 uint32_t TX 카운터 위치.
 * info->payload 는 vendor action frame body 시작 — Category 1 + OUI 3 + Random 4 + IE헤더 7 = 15.
 * esp32s3_csi_send_poc 가 4바이트(uint32 count)를 실으므로 payload_len 은 정확히 19다.
 * 즉 아래 길이 검사는 경계값에 딱 걸린다 — TX payload 크기를 줄이면 tx_seq 가 조용히 0이 된다. */
#define CSI_TX_SEQ_OFFSET        15
/* IDENT payload: MAC 6B + 펌웨어 문자열 10B + 진단 카운터 4×u32 = 32B.
 * 진단을 프레임에 실어야 하는 이유: 이 프로젝트의 콘솔 primary 는 GPIO43 UART 이고
 * (CONFIG_ESP_CONSOLE_UART_CUSTOM), USB-Serial-JTAG 드라이버를 직접 설치해 쓰기 때문에
 * ESP_LOG 가 USB 로 나오지 않는다. 실측 확인(2026-08-25): 호스트에서 17초 캡처 중
 * 로그 텍스트 0건. 즉 5초 로그만으로는 ringbuf_drop/partial 을 볼 방법이 없었다. */
#define CSI_IDENT_PAYLOAD_LEN    32
#define CSI_IDENT_PERIOD_MS      2000

#pragma pack(push, 1)
typedef struct {
    uint16_t magic;          /* 0x4353 */
    uint8_t  version;        /* 3 */
    uint8_t  frame_type;     /* 0=CSI, 1=IDENT */
    uint16_t total_len;      /* 헤더 + payload 합산 길이 */
    uint16_t raw_len;        /* payload 바이트 수 (HT20 LLTF = 128) */
    uint32_t seq;            /* RX 부팅부터 단조 증가 (보드별 독립) */
    uint64_t timestamp_us;   /* RX esp_timer_get_time() (보드별 독립) */
    int8_t   rssi;
    uint8_t  channel;
    int8_t   noise_floor;
    uint8_t  rate;
    uint16_t sig_len;
    uint16_t boot_id;        /* 부팅마다 새 값 — seq 되감김(재부팅) vs 보드 혼입 구분용 */
    uint32_t tx_seq;         /* TX 송신 카운터 (모든 RX 공통 — cross-RX 동기화 키) */
    uint8_t  agc_gain;       /* AGC gain (진단·재현용 원값) */
    int8_t   fft_gain;       /* FFT gain */
    uint16_t rx_id;          /* 업링크 모드: CSI_RX_ID. 싱크가 여러 RX 프레임을 한 스트림으로
                              * 넘길 때 host 가 이걸로 device 를 가른다. USB 직결은 0 */
    float    gain_comp;      /* 진폭 gain 보정 배율. 0 = baseline 미완성(첫 100패킷) */
    uint32_t crc32;          /* 헤더(이 필드를 0으로 둔 상태) + payload 전체 */
    /* payload[raw_len] tail */
} csi_frame_header_t;
#pragma pack(pop)
_Static_assert(sizeof(csi_frame_header_t) == 44, "csi_frame_header_t must be 44 bytes");
/* 아래 오프셋은 scripts/csi_store.py 의 HEADER_DTYPE 과 짝이다. 한쪽만 고치면 여기서 터진다. */
_Static_assert(offsetof(csi_frame_header_t, seq)          ==  8, "seq offset");
_Static_assert(offsetof(csi_frame_header_t, timestamp_us) == 12, "timestamp_us offset");
_Static_assert(offsetof(csi_frame_header_t, boot_id)      == 26, "boot_id offset");
_Static_assert(offsetof(csi_frame_header_t, tx_seq)       == 28, "tx_seq offset");
_Static_assert(offsetof(csi_frame_header_t, agc_gain)     == 32, "agc_gain offset");
_Static_assert(offsetof(csi_frame_header_t, gain_comp)    == 36, "gain_comp offset");
_Static_assert(offsetof(csi_frame_header_t, crc32)        == 40, "crc32 offset");

static RingbufHandle_t g_csi_ringbuf = NULL;
static uint32_t g_frame_seq = 0;
static uint16_t g_boot_id = 0;
static uint8_t  g_base_mac[6] = {0};

/* zlib 호환 CRC-32 (reflected, poly 0xEDB88320), 16엔트리 니블 테이블.
 * ROM의 esp_rom_crc32_le 는 IDF 버전마다 pre/post inversion 관례가 달라 호스트
 * zlib.crc32 와 맞추기 까다로워 직접 구현한다. 168B × 100Hz 는 무시할 부하다. */
static const uint32_t CRC32_NIBBLE[16] = {
    0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC,
    0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C,
    0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C,
    0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C,
};

static uint32_t csi_crc32(const uint8_t *buf, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= buf[i];
        crc = (crc >> 4) ^ CRC32_NIBBLE[crc & 0x0f];
        crc = (crc >> 4) ^ CRC32_NIBBLE[crc & 0x0f];
    }
    return ~crc;
}

/* 헤더+payload 를 buf 에 조립하고 crc32 를 채운 뒤 ring buffer 로 push. */
static void csi_frame_push(csi_frame_header_t *hdr, const void *payload, size_t payload_len)
{
    uint8_t buf[sizeof(csi_frame_header_t) + CSI_MAX_RAW_BYTES];
    size_t total = sizeof(*hdr) + payload_len;

    hdr->magic     = CSI_FRAME_MAGIC;
    hdr->version   = CSI_FRAME_VERSION;
    hdr->total_len = (uint16_t)total;
    hdr->raw_len   = (uint16_t)payload_len;
    hdr->boot_id   = g_boot_id;
#if CSI_UPLINK_ENABLED
    hdr->rx_id     = CSI_RX_ID;
#else
    hdr->rx_id     = 0;
#endif
    hdr->crc32     = 0;

    memcpy(buf, hdr, sizeof(*hdr));
    if (payload_len) {
        memcpy(buf + sizeof(*hdr), payload, payload_len);
    }
    uint32_t crc = csi_crc32(buf, total);
    memcpy(buf + offsetof(csi_frame_header_t, crc32), &crc, sizeof(crc));

    /* 가득 차면 **가장 오래된 항목을 버리고** 새 프레임을 넣는다 (keep-newest).
     * 기본 ringbuf 는 가득 차면 새 것을 버리는데, 그러면 호스트가 안 붙어 있는 동안
     * 옛 프레임이 버퍼를 점유해 수집을 시작한 순간 수십 초 묵은 데이터가 먼저 흘러나온다.
     * 실측(2026-08-25): 46초 방치 후 수집하니 앞부분이 통째로 옛 프레임이라
     * tx_seq 격자에 4134스텝 구멍이 생겼다. */
    for (int retry = 0; retry < 4; ++retry) {
        if (xRingbufferSend(g_csi_ringbuf, buf, total, 0) == pdTRUE) {
            return;
        }
        size_t old_len = 0;
        void *old = xRingbufferReceive(g_csi_ringbuf, &old_len, 0);
        if (!old) {
            break;
        }
        vRingbufferReturnItem(g_csi_ringbuf, old);
        g_ringbuf_drop++;
    }
    g_ringbuf_drop++;
}

#define CONFIG_LESS_INTERFERENCE_CHANNEL   11
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE               WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
/* HT20: 64 OFDM 서브캐리어, raw CSI 128B. MeshSense 학습 모델이 64 SC 기준이라 사용.
 * TX 측(esp32s3_csi_send_poc)도 동일 HT20으로 맞춰야 ESP-NOW peer rate 협상 일치. */
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT20
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HT20
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                   0

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
#define CSI_FORCE_LLTF                      0
#endif

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define CONFIG_GAIN_CONTROL                 1
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

/* 역할별 MAC. 모두 같은 값을 쓰면 RX 가 업링크를 시작하는 순간 서로의 프레임을 CSI 로
 * 잡아 자기오염된다. TX MAC 만 CSI 필터를 통과시킨다.
 *   TX   1a:00:00:00:00:00   (자극원 — CSI 필터 기준)
 *   RX   1a:00:00:00:00:<id> (CSI_RX_ID, 1~254)
 *   SINK 1a:00:00:00:00:ff   (업링크 수신자) */
static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
#if CSI_UPLINK_ENABLED
static const uint8_t CSI_SINK_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0xff};
static uint8_t g_rx_mac[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, CSI_RX_ID};
static volatile uint32_t g_uplink_ok = 0;
static volatile uint32_t g_uplink_fail = 0;
#endif
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

    /* STA MAC 을 송신자와 같은 값으로 맞추는 것은 esp-csi 예제의 association-free 트릭이다.
     * RX 는 아무것도 송신하지 않으므로 충돌하지 않는다. 실시간 경로(ESP-NOW 업링크)로 갈 때는
     * RX 마다 다른 MAC 을 줘야 서로의 업링크를 CSI 로 잡지 않는다. */
#if CSI_UPLINK_ENABLED
    /* 업링크 모드: 자기 MAC 을 따로 쓴다. CSI 필터는 여전히 TX MAC 기준이라
     * 싱크로 보낸 자기 프레임이나 다른 RX 의 업링크는 CSI 로 잡히지 않는다. */
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, g_rx_mac));
#else
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
#endif
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
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
    g_csi_recv_count++;

    if (!g_csi_ringbuf) {
        return;
    }

    size_t raw_len = info->len;
    if (raw_len > CSI_MAX_RAW_BYTES) raw_len = CSI_MAX_RAW_BYTES;

    uint32_t tx_seq = 0;
    if (info->payload && info->payload_len >= CSI_TX_SEQ_OFFSET + 4) {
        memcpy(&tx_seq, info->payload + CSI_TX_SEQ_OFFSET, 4);
    }

    /* AGC 가 게인을 바꾸면 raw 진폭이 계단식으로 뛴다 — 모델이 그걸 움직임으로 오인한다.
     * 실측(RX103, 60초): AGC 5~7단계, FFT 8~23단계가 실제로 변한다.
     * 보정 배율은 반드시 여기서 계산해야 한다. esp_csi_gain_ctrl 은 소스 없는 정적
     * 라이브러리로만 배포되어 호스트에서 같은 식을 재현할 방법이 없다.
     * 첫 100패킷은 baseline 수집 구간이라 gain_comp=0 (호스트가 "보정 불가"로 읽는다). */
    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    float gain_comp = 0.0f;
#if CONFIG_GAIN_CONTROL
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);
    static uint32_t s_gain_samples = 0;
    if (s_gain_samples < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
        s_gain_samples++;
    } else if (esp_csi_gain_ctrl_get_gain_compensation(&gain_comp, agc_gain, fft_gain) != ESP_OK) {
        gain_comp = 0.0f;
    }
#endif

    csi_frame_header_t hdr = {
        .frame_type   = CSI_FRAME_TYPE_CSI,
        .seq          = g_frame_seq++,
        .timestamp_us = (uint64_t)esp_timer_get_time(),
        .rssi         = info->rx_ctrl.rssi,
        .channel      = info->rx_ctrl.channel,
        .noise_floor  = info->rx_ctrl.noise_floor,
        .rate         = (uint8_t)info->rx_ctrl.rate,
        .sig_len      = (uint16_t)info->rx_ctrl.sig_len,
        .tx_seq       = tx_seq,
        .agc_gain     = agc_gain,
        .fft_gain     = fft_gain,
        .gain_comp    = gain_comp,
    };
    csi_frame_push(&hdr, info->buf, raw_len);
}

/* 보드 자기소개. 호스트는 이 프레임의 eFuse base MAC 으로 device_id 를 결정하므로
 * 수집 시작 전에 esptool 로 포트를 프로브(=보드 리셋)할 필요가 없다. */
static void ident_task(void *arg)
{
    (void)arg;
    uint8_t payload[CSI_IDENT_PAYLOAD_LEN] = {0};
    memcpy(payload, g_base_mac, 6);
    strncpy((char *)payload + 6, "meshsense", 10);

    while (1) {
        uint32_t counters[4] = {
            g_csi_recv_count,
#if CSI_UPLINK_ENABLED
            /* 업링크 모드에서 uart_* 는 의미가 없다. 대신 업링크 성공/실패를 싣는다 —
             * 손실이 RX 송신에서 나는지 sink·USB 에서 나는지 구분하려면 이 값이 필요하다. */
            g_uplink_ok, g_ringbuf_drop, g_uplink_fail,
#else
            g_uart_send_count, g_ringbuf_drop, g_uart_partial,
#endif
        };
        memcpy(payload + 16, counters, sizeof(counters));

        csi_frame_header_t hdr = {
            .frame_type   = CSI_FRAME_TYPE_IDENT,
            .seq          = g_frame_seq,   /* IDENT 는 seq 를 증가시키지 않는다 */
            .timestamp_us = (uint64_t)esp_timer_get_time(),
        };
        if (g_csi_ringbuf) {
            csi_frame_push(&hdr, payload, sizeof(payload));
        }
        vTaskDelay(pdMS_TO_TICKS(CSI_IDENT_PERIOD_MS));
    }
}

/* 5초마다 누적 카운트와 직전 5초 Hz를 로그. */
static void hz_log_task(void *arg)
{
    (void)arg;
    uint32_t prev_cb = 0, prev_uart = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        uint32_t cb = g_csi_recv_count;
        uint32_t up = g_uart_send_count;
        /* ESP_LOG 는 USB-Serial-JTAG 로도 나가 바이너리 스트림에 끼어든다
         * (CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y). 호스트가 magic+CRC 로
         * 재동기화하므로 무해하지만, 엄격히 하려면 CONFIG_LOG_DEFAULT_LEVEL_NONE=y. */
#if CSI_UPLINK_ENABLED
        ESP_LOGI(TAG, "5s: cb=%" PRIu32 " (+%" PRIu32 ", %.1fHz) uplink_ok=%" PRIu32
                       " fail=%" PRIu32 " ringbuf_drop=%" PRIu32,
                 cb, cb - prev_cb, (cb - prev_cb) / 5.0f,
                 g_uplink_ok, g_uplink_fail, g_ringbuf_drop);
        (void)up; (void)prev_uart;
#else
        ESP_LOGI(TAG, "5s: cb=%" PRIu32 " (+%" PRIu32 ", %.1fHz) uart=%" PRIu32
                       " (+%" PRIu32 ", %.1fHz) ringbuf_drop=%" PRIu32 " partial=%" PRIu32,
                 cb, cb - prev_cb, (cb - prev_cb) / 5.0f,
                 up, up - prev_uart, (up - prev_uart) / 5.0f,
                 g_ringbuf_drop, g_uart_partial);
#endif
        prev_cb = cb;
        prev_uart = up;
    }
}

#if !CSI_UPLINK_ENABLED
/* USB-Serial-JTAG writer task: ring buffer에서 꺼내 USB-CDC로 그대로 쓴다.
 * ESP32-S3 dev 보드의 USB-C는 UART0가 아니라 USB-Serial-JTAG에 연결되어 있다. */
static void uart_writer_task(void *arg)
{
    (void)arg;
    while (1) {
        size_t len = 0;
        uint8_t *p = (uint8_t *)xRingbufferReceive(g_csi_ringbuf, &len, portMAX_DELAY);
        if (!p) continue;

        /* ring buffer 의 4바이트 정렬 패딩을 빼고 헤더가 선언한 길이만 보낸다. */
        if (len >= sizeof(csi_frame_header_t)) {
            uint16_t declared = ((csi_frame_header_t *)p)->total_len;
            if (declared >= sizeof(csi_frame_header_t) && declared <= len) {
                len = declared;
            }
        }
        /* 부분 write 를 반드시 이어서 보낸다. 잔여분을 버리면 스트림에 잘린 프레임이
         * 남아 호스트가 재동기화해야 하고, 그 과정에서 오탐 magic 위험이 커진다. */
        size_t off = 0;
        while (off < len) {
            int w = usb_serial_jtag_write_bytes(p + off, len - off, pdMS_TO_TICKS(100));
            if (w <= 0) {
                break;      /* 호스트가 포트를 안 읽는 중 — 이 프레임은 포기 */
            }
            off += (size_t)w;
            if (off < len) {
                g_uart_partial++;
            }
        }
        vRingbufferReturnItem(g_csi_ringbuf, p);
        if (off == len) {
            g_uart_send_count++;
        }
    }
}

#endif  /* !CSI_UPLINK_ENABLED */

#if CSI_UPLINK_ENABLED
/* 업링크 writer: ring buffer 에서 꺼내 ESP-NOW 로 싱크에 보낸다.
 * USB 를 쓰지 않는다 — RX 는 무선 배치가 목적이라 host 연결을 전제하지 않는다.
 *
 * unicast 를 쓰는 이유: broadcast 는 ACK·재전송이 없어 손실을 측정할 수는 있어도
 * 복구할 수 없다. 우선 unicast 로 신뢰성을 확보하고, 에어타임이 CSI 콜백을 방해하면
 * 그때 broadcast·양자화·묶음전송을 검토한다.
 *
 * esp_now_send 는 이전 전송이 끝나기 전에 다시 부르면 ESP_ERR_ESPNOW_NO_MEM 을 낸다.
 * send 콜백으로 완료를 기다린 뒤 다음 프레임을 보낸다. */
static SemaphoreHandle_t g_uplink_done = NULL;

static void uplink_send_cb(const uint8_t *mac, esp_now_send_status_t status)
{
    (void)mac;
    if (status == ESP_NOW_SEND_SUCCESS) {
        g_uplink_ok++;
    } else {
        g_uplink_fail++;
    }
    BaseType_t hp = pdFALSE;
    xSemaphoreGiveFromISR(g_uplink_done, &hp);
    if (hp) portYIELD_FROM_ISR();
}

#if CSI_UPLINK_OFFSET_MS > 0
static esp_timer_handle_t g_offset_timer;
static TaskHandle_t g_uplink_task;

static void offset_timer_cb(void *arg)
{
    (void)arg;
    if (g_uplink_task) xTaskNotifyGive(g_uplink_task);
}
#endif

static void uplink_writer_task(void *arg)
{
    (void)arg;
#if CSI_UPLINK_OFFSET_MS > 0
    g_uplink_task = xTaskGetCurrentTaskHandle();
    const esp_timer_create_args_t targs = { .callback = offset_timer_cb, .name = "ul_off" };
    ESP_ERROR_CHECK(esp_timer_create(&targs, &g_offset_timer));
#endif
    while (1) {
        size_t len = 0;
        uint8_t *p = (uint8_t *)xRingbufferReceive(g_csi_ringbuf, &len, portMAX_DELAY);
        if (!p) continue;
        /* ring buffer 는 NOSPLIT 항목을 4바이트 정렬 크기로 돌려준다 — 172바이트 프레임에
         * len=176 이 온다. 그대로 보내면 프레임마다 4바이트 쓰레기가 붙어 host 가 매번
         * 재동기화한다. 프레임 길이는 헤더가 스스로 들고 있으니 그것을 쓴다. */
        size_t total = len;
        if (len >= sizeof(csi_frame_header_t)) {
            uint16_t declared = ((csi_frame_header_t *)p)->total_len;
            if (declared >= sizeof(csi_frame_header_t) && declared <= len) {
                total = declared;
            }
        }
#if CSI_UPLINK_OFFSET_MS > 0
        /* 송신 시점 분산: 캡처 시각 기준 rx_id × OFFSET 뒤에 보낸다. 캡처 시각 기준이라
         * 백로그로 늦게 꺼낸 프레임은 wait<=0 → 즉시 전송, 지연이 누적되지 않는다.
         * tick 이 10ms 라 vTaskDelay 로는 ms 단위를 못 맞춘다 → esp_timer one-shot + task notify.
         * (busy-wait 로도 결과는 같았다. 오프셋 RX 의 캡처가 ~1% 주는 건 CPU 가 아니라 송신
         * 시점 자체의 비용 — troubleshooting/08.) 상한 20ms 로 폭주 방지. */
        if (len >= sizeof(csi_frame_header_t)) {
            const csi_frame_header_t *h = (const csi_frame_header_t *)p;
            int64_t target = (int64_t)h->timestamp_us + (int64_t)CSI_RX_ID * CSI_UPLINK_OFFSET_MS * 1000;
            int64_t wait = target - esp_timer_get_time();
            if (wait > 0) {
                if (wait > 20000) wait = 20000;
                ulTaskNotifyTake(pdTRUE, 0);                 /* 묵은 notify 제거 */
                esp_timer_start_once(g_offset_timer, (uint64_t)wait);
                ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(30));
            }
        }
#endif
        if (total <= ESP_NOW_MAX_DATA_LEN) {
            if (esp_now_send(CSI_SINK_MAC, p, total) == ESP_OK) {
                /* 완료를 기다린다. 싱크가 없으면 콜백이 fail 로 오므로 멈추지 않는다. */
                xSemaphoreTake(g_uplink_done, pdMS_TO_TICKS(100));
            } else {
                g_uplink_fail++;
            }
        } else {
            g_uplink_fail++;      /* 프레임이 ESP-NOW 상한(250B)을 넘음 */
        }
        vRingbufferReturnItem(g_csi_ringbuf, p);
    }
}
#endif  /* CSI_UPLINK_ENABLED */

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
    /* MeshSense 모델은 64 SC 기준. ESP32-S3 CSI HW는 lltf+htltf 둘 다 켜면
     * HT20에서도 LLTF(64) + HT-LTF(64)를 concatenate해 128 SC × I/Q = 256B를 낸다.
     * → htltf 끔으로 LLTF 64 SC × I/Q = 128B 로 통일. */
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = false,  /* HT-LTF 끔 → 64 SC LLTF only */
        .stbc_htltf2_en    = false,
        .ltf_merge_en      = false,
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
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* IDENT 로 알릴 보드 고유 MAC. esp_wifi_set_mac 으로 덮어쓸 STA MAC 이 아니라
     * eFuse base MAC 이어야 esptool read_mac / device_registry.csv 의 sta_mac 과 일치한다. */
    ESP_ERROR_CHECK(esp_efuse_mac_get_default(g_base_mac));
    g_boot_id = (uint16_t)(esp_random() & 0xFFFF);

    wifi_init();

    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    wifi_esp_now_init(peer);

#if !CSI_UPLINK_ENABLED
    /* USB-Serial-JTAG 드라이버 설치. ESP32-S3 dev 보드 USB-C가 여기로 연결됨.
     * 업링크 모드에서는 설치하지 않는다 — RX 는 USB 로 데이터를 내보내지 않는다. */
    usb_serial_jtag_driver_config_t usj_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usj_cfg.tx_buffer_size = CSI_USJ_TX_BUF_BYTES;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usj_cfg));
#endif

    g_csi_ringbuf = xRingbufferCreate(CSI_RINGBUF_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!g_csi_ringbuf) {
        ESP_LOGE(TAG, "ring buffer alloc failed");
    } else {
#if CSI_UPLINK_ENABLED
        g_uplink_done = xSemaphoreCreateBinary();
        ESP_ERROR_CHECK(esp_now_register_send_cb(uplink_send_cb));
        esp_now_peer_info_t sink = {
            .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
            .ifidx = WIFI_IF_STA,
            .encrypt = false,
        };
        memcpy(sink.peer_addr, CSI_SINK_MAC, 6);
        ESP_ERROR_CHECK(esp_now_add_peer(&sink));
        esp_now_rate_config_t rate = {.phymode = CONFIG_ESP_NOW_PHYMODE,
                                      .rate = CONFIG_ESP_NOW_RATE, .ersu = false, .dcm = false};
        ESP_ERROR_CHECK(esp_now_set_peer_rate_config(sink.peer_addr, &rate));
        xTaskCreate(uplink_writer_task, "uplink", 4096, NULL, 5, NULL);
#else
        xTaskCreate(uart_writer_task, "uart_writer", 4096, NULL, 5, NULL);
#endif
        xTaskCreate(ident_task, "ident", 3072, NULL, 4, NULL);
    }

    wifi_csi_init();

    ESP_LOGI(TAG, "================ CSI RECV ================");
#if CSI_UPLINK_ENABLED
    ESP_LOGI(TAG, "frame v%d, boot_id=%u, base_mac=" MACSTR " | UPLINK rx_id=%d offset=%dms → sink " MACSTR,
             CSI_FRAME_VERSION, g_boot_id, MAC2STR(g_base_mac), CSI_RX_ID,
             CSI_RX_ID * CSI_UPLINK_OFFSET_MS, MAC2STR(CSI_SINK_MAC));
#else
    ESP_LOGI(TAG, "frame v%d, boot_id=%u, base_mac=" MACSTR " | USB",
             CSI_FRAME_VERSION, g_boot_id, MAC2STR(g_base_mac));
#endif

    xTaskCreate(hz_log_task, "hz_log", 3072, NULL, 4, NULL);
}
