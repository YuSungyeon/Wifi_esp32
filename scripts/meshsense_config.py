"""MeshSense 통합 플래시 설정 (TX/RX 공통 SSOT)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "meshsense_config.json"
EXAMPLE_CONFIG_PATH = SCRIPT_DIR / "meshsense_config.example.json"


@dataclass(frozen=True)
class MeshSenseConfig:
    ap_ssid: str
    ap_pass: str
    ap_channel: int
    ap_max_conn: int
    ap_broadcast_port: int
    ap_interval_ms: int
    ap_payload_bytes: int
    collector_ip: str
    collector_port: int

    def rx_cmake_defines(self, device_id: int) -> List[str]:
        return [
            f"-DCSI_WIFI_SSID={self.ap_ssid}",
            f"-DCSI_WIFI_PASS={self.ap_pass}",
            f"-DCSI_COLLECTOR_IP={self.collector_ip}",
            f"-DCSI_COLLECTOR_PORT={self.collector_port}",
            f"-DCSI_DEVICE_ID={device_id}",
        ]

    def tx_cmake_defines(self, tx_node_id: int) -> List[str]:
        return [
            f"-DTX_AP_SSID={self.ap_ssid}",
            f"-DTX_AP_PASS={self.ap_pass}",
            f"-DTX_AP_CHANNEL={self.ap_channel}",
            f"-DTX_AP_MAX_CONN={self.ap_max_conn}",
            f"-DTX_AP_BROADCAST_PORT={self.ap_broadcast_port}",
            f"-DTX_AP_INTERVAL_MS={self.ap_interval_ms}",
            f"-DTX_AP_PAYLOAD_BYTES={self.ap_payload_bytes}",
            f"-DTX_AP_NODE_ID={tx_node_id}",
        ]


def _require_mapping(data: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
    val = data.get(key)
    if not isinstance(val, dict):
        raise ValueError(f"{path}.{key} must be an object")
    return val


def _parse_unified(data: Dict[str, Any]) -> MeshSenseConfig:
    ap = _require_mapping(data, "ap", "root")
    collector = _require_mapping(data, "collector", "root")

    def req_str(obj: Dict[str, Any], key: str, prefix: str) -> str:
        if key not in obj:
            raise ValueError(f"{prefix}.{key} is required")
        return str(obj[key])

    def req_int(obj: Dict[str, Any], key: str, prefix: str) -> int:
        if key not in obj:
            raise ValueError(f"{prefix}.{key} is required")
        return int(obj[key])

    return MeshSenseConfig(
        ap_ssid=req_str(ap, "ssid", "ap"),
        ap_pass=req_str(ap, "pass", "ap"),
        ap_channel=req_int(ap, "channel", "ap"),
        ap_max_conn=req_int(ap, "max_conn", "ap"),
        ap_broadcast_port=req_int(ap, "broadcast_port", "ap"),
        ap_interval_ms=req_int(ap, "interval_ms", "ap"),
        ap_payload_bytes=req_int(ap, "payload_bytes", "ap"),
        collector_ip=req_str(collector, "ip", "collector"),
        collector_port=req_int(collector, "port", "collector"),
    )


def load_meshsense_config(path: Path = DEFAULT_CONFIG_PATH) -> MeshSenseConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"config not found: {path}\n"
            f"  cp {EXAMPLE_CONFIG_PATH} {DEFAULT_CONFIG_PATH}\n"
            f"  edit ap/collector for your lab (Mac IP on TX SoftAP: ipconfig getifaddr en0)"
        )
    with path.open("r", encoding="utf-8") as f:
        return _parse_unified(json.load(f))
