"""Native pure-Python implementation of the SiliconDust HDHomeRun control protocol (port 65001).

Implements the same binary TLV packet format and CRC32 framing used by `libhdhomerun`
and `hdhomerun_config`, enabling UDP LAN broadcast discovery and direct TCP tuner query/control
without requiring external CLI binaries.
"""

from __future__ import annotations

import re
import socket
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


HDHOMERUN_CONTROL_PORT = 65001

# Packet types
TYPE_DISCOVER_REQ = 0x0002
TYPE_DISCOVER_RPY = 0x0003
TYPE_GETSET_REQ = 0x0004
TYPE_GETSET_RPY = 0x0005

# TLV Tags
TAG_DEVICE_TYPE = 0x01
TAG_DEVICE_ID = 0x02
TAG_GETSET_NAME = 0x03
TAG_GETSET_VALUE = 0x04
TAG_GETSET_LOCKKEY = 0x15
TAG_ERROR_MESSAGE = 0x05
TAG_TUNER_COUNT = 0x10
TAG_BASE_URL = 0x27
TAG_LINEUP_URL = 0x28
TAG_DEVICE_AUTH_STR = 0x2A

DEVICE_TYPE_TUNER = 0x00000001
DEVICE_TYPE_WILDCARD = 0xFFFFFFFF
DEVICE_ID_WILDCARD = 0xFFFFFFFF


@dataclass
class TunerControlStatus:
    """Detailed RF and transport stream telemetry from port 65001 `/tunerN/status`."""
    tuner_index: int
    raw_status: str = ""
    raw_vstatus: str = ""
    raw_streaminfo: str = ""
    channel: str = "none"
    lock: str = "none"
    signal_strength: int = 0      # ss (0-100%)
    signal_to_noise: int = 0      # snq (0-100%)
    symbol_error_quality: int = 0 # seq (0-100%)
    bits_per_second: int = 0      # bps
    packets_per_second: int = 0   # pps
    vchannel: str = ""
    vname: str = ""
    frequency_hz: int = 0
    programs: List[str] = field(default_factory=list)


def _encode_tlv(tag: int, value_bytes: bytes) -> bytes:
    length = len(value_bytes)
    if length < 128:
        return struct.pack("BB", tag, length) + value_bytes
    return struct.pack("BBB", tag, 0x80 | (length & 0x7F), (length >> 7) & 0xFF) + value_bytes


def _decode_tlvs(payload: bytes) -> Dict[int, bytes]:
    result: Dict[int, bytes] = {}
    idx = 0
    n = len(payload)
    while idx + 2 <= n:
        tag = payload[idx]
        length = payload[idx + 1]
        idx += 2
        if length & 0x80:
            if idx >= n:
                break
            length = (length & 0x7F) | (payload[idx] << 7)
            idx += 1
        if idx + length > n:
            break
        result[tag] = payload[idx : idx + length]
        idx += length
    return result


def _build_packet(pkt_type: int, payload: bytes) -> bytes:
    header = struct.pack(">HH", pkt_type, len(payload))
    body = header + payload
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("<I", crc)


def _parse_packet(data: bytes) -> Optional[Tuple[int, Dict[int, bytes]]]:
    if len(data) < 8:
        return None
    pkt_type, length = struct.unpack(">HH", data[:4])
    if len(data) < 4 + length + 4:
        return None
    body = data[: 4 + length]
    expected_crc = struct.unpack("<I", data[4 + length : 4 + length + 4])[0]
    actual_crc = zlib.crc32(body) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        return None
    return pkt_type, _decode_tlvs(data[4 : 4 + length])


def udp_discover_devices(timeout: float = 1.2) -> List[Dict[str, str]]:
    """Broadcast an HDHomeRun UDP discovery packet on port 65001 and return discovered devices."""
    payload = (
        _encode_tlv(TAG_DEVICE_TYPE, struct.pack(">I", DEVICE_TYPE_TUNER))
        + _encode_tlv(TAG_DEVICE_ID, struct.pack(">I", DEVICE_ID_WILDCARD))
    )
    pkt = _build_packet(TYPE_DISCOVER_REQ, payload)

    discovered: Dict[str, Dict[str, str]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        try:
            sock.sendto(pkt, ("255.255.255.255", HDHOMERUN_CONTROL_PORT))
        except OSError:
            pass

        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                break

            parsed = _parse_packet(data)
            if not parsed:
                continue
            pkt_type, tlvs = parsed
            if pkt_type != TYPE_DISCOVER_RPY:
                continue

            ip = addr[0]
            dev_id_bytes = tlvs.get(TAG_DEVICE_ID, b"")
            dev_id = (
                f"{struct.unpack('>I', dev_id_bytes)[0]:08X}"
                if len(dev_id_bytes) == 4
                else ip
            )
            base_url = tlvs.get(TAG_BASE_URL, b"").rstrip(b"\x00").decode("utf-8", errors="ignore")
            if not base_url:
                base_url = f"http://{ip}"
            lineup_url = tlvs.get(TAG_LINEUP_URL, b"").rstrip(b"\x00").decode("utf-8", errors="ignore")
            if not lineup_url:
                lineup_url = f"{base_url}/lineup.json"
            tuner_count_b = tlvs.get(TAG_TUNER_COUNT, b"")
            tuner_count = str(tuner_count_b[0]) if tuner_count_b else ""

            discovered[dev_id] = {
                "DeviceID": dev_id,
                "LocalIP": ip,
                "BaseURL": base_url,
                "DiscoverURL": f"{base_url}/discover.json",
                "LineupURL": lineup_url,
                "TunerCount": tuner_count,
            }
    finally:
        sock.close()

    return list(discovered.values())


def getset_var(ip: str, name: str, value: Optional[str] = None, timeout: float = 2.0) -> Tuple[Optional[str], Optional[str]]:
    """Query or set a control variable on the HDHomeRun via TCP port 65001.

    Returns (value_str, error_str).
    """
    payload = _encode_tlv(TAG_GETSET_NAME, name.encode("ascii") + b"\x00")
    if value is not None:
        payload += _encode_tlv(TAG_GETSET_VALUE, value.encode("ascii") + b"\x00")
    pkt = _build_packet(TYPE_GETSET_REQ, payload)

    try:
        with socket.create_connection((ip, HDHOMERUN_CONTROL_PORT), timeout=timeout) as sock:
            sock.sendall(pkt)
            resp = sock.recv(8192)
    except OSError as exc:
        return None, str(exc)

    parsed = _parse_packet(resp)
    if not parsed:
        return None, "Invalid response packet from HDHomeRun"
    pkt_type, tlvs = parsed
    if pkt_type != TYPE_GETSET_RPY:
        return None, f"Unexpected packet type: {pkt_type:#x}"

    if TAG_ERROR_MESSAGE in tlvs:
        err = tlvs[TAG_ERROR_MESSAGE].rstrip(b"\x00").decode("utf-8", errors="replace")
        return None, err

    val = tlvs.get(TAG_GETSET_VALUE, b"").rstrip(b"\x00").decode("utf-8", errors="replace")
    return val, None


def parse_tuner_status_string(tuner_index: int, status_str: str, vstatus_str: str = "", streaminfo_str: str = "") -> TunerControlStatus:
    """Parse `/tunerN/status` (e.g. `ch=8vsb:599000000 lock=8vsb ss=87 snq=65 seq=100 bps=19392712 pps=1285`)."""
    res = TunerControlStatus(
        tuner_index=tuner_index,
        raw_status=status_str.strip(),
        raw_vstatus=vstatus_str.strip(),
        raw_streaminfo=streaminfo_str.strip(),
    )

    tokens = status_str.strip().split()
    for tok in tokens:
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if k == "ch":
            res.channel = v
            # Extract frequency if present (e.g. 8vsb:599000000 or auto:599000000)
            m = re.search(r":(\d{6,10})$", v)
            if m:
                res.frequency_hz = int(m.group(1))
        elif k == "lock":
            res.lock = v
        elif k == "ss":
            try:
                res.signal_strength = int(v)
            except ValueError:
                pass
        elif k == "snq":
            try:
                res.signal_to_noise = int(v)
            except ValueError:
                pass
        elif k == "seq":
            try:
                res.symbol_error_quality = int(v)
            except ValueError:
                pass
        elif k == "bps":
            try:
                res.bits_per_second = int(v)
            except ValueError:
                pass
        elif k == "pps":
            try:
                res.packets_per_second = int(v)
            except ValueError:
                pass

    # Parse vstatus (e.g. `vch=3.1 name=KCRA-TV auth=subscribed cci=none cgms=none`)
    if vstatus_str:
        vch_match = re.search(r"vch=(\S+)", vstatus_str)
        if vch_match and vch_match.group(1) != "none":
            res.vchannel = vch_match.group(1)
        name_match = re.search(r"name=(.+?)(?:\s+auth=|$)", vstatus_str)
        if name_match and name_match.group(1) != "none":
            res.vname = name_match.group(1).strip()

    # Parse streaminfo lines (e.g. `3: 3.1 KCRA-TV` or `tsid=0x...`)
    if streaminfo_str:
        lines = [
            ln.strip()
            for ln in streaminfo_str.splitlines()
            if ln.strip() and not ln.strip().startswith("tsid=")
        ]
        res.programs = lines

    return res


def query_tuner_detailed_status(ip: str, tuner_index: int = 0, timeout: float = 1.5) -> TunerControlStatus:
    """Query `/tunerN/status`, `/tunerN/vstatus`, and `/tunerN/streaminfo` from the device."""
    status_str, _ = getset_var(ip, f"/tuner{tuner_index}/status", timeout=timeout)
    vstatus_str, _ = getset_var(ip, f"/tuner{tuner_index}/vstatus", timeout=timeout)
    streaminfo_str, _ = getset_var(ip, f"/tuner{tuner_index}/streaminfo", timeout=timeout)
    return parse_tuner_status_string(
        tuner_index=tuner_index,
        status_str=status_str or "",
        vstatus_str=vstatus_str or "",
        streaminfo_str=streaminfo_str or "",
    )

