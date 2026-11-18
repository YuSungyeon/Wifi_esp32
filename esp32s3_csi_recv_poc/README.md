# MeshSense CSI RX Firmware

현재 공식 RX firmware입니다. 디렉터리의 `_poc` 이름은 역사적으로 유지합니다.
USB 직결(기본, `CSI_UPLINK_ENABLED=0`)과 ESP-NOW 업링크(`CSI_UPLINK_ENABLED=1`) 두 모드로 빌드합니다.

Binary data contract: [doc/data-schema.md](../doc/data-schema.md)
동작과 build 절차: [doc/firmware.md](../doc/firmware.md) · 업링크 모드: [doc/realtime-uplink.md](../doc/realtime-uplink.md)
