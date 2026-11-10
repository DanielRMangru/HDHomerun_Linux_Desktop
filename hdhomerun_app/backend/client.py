"""HDHomeRun device discovery, lineup management, scanning, and live RF signal client."""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

from ..config import AppConfig
from .protocol import (
    TunerControlStatus,
    getset_var,
    query_tuner_detailed_status,
    udp_discover_devices,
)


DISCOVER_CLOUD_URL = "https://ipv4-api.hdhomerun.com/discover"


@dataclass
class HDHomeRunDevice:
    """Metadata and connection endpoints for a discovered HDHomeRun device."""
    device_id: str
    local_ip: str
    base_url: str
    discover_url: str
    lineup_url: str
    friendly_name: str = "HDHomeRun"
    model_number: str = ""
    firmware_name: str = ""
    firmware_version: str = ""
    device_auth: str = ""
    tuner_count: int = 2
    source: str = "Antenna"
    source_list: List[str] = field(default_factory=lambda: ["Antenna", "Cable"])

    @property
    def display_title(self) -> str:
        model = f" ({self.model_number})" if self.model_number else ""
        return f"{self.friendly_name}{model} — {self.local_ip} [{self.device_id}]"


@dataclass
class ChannelItem:
    """Unified channel representation combining hardware lineup and local overrides."""
    guide_number: str
    guide_name: str
    url: str
    video_codec: str = ""
    audio_codec: str = ""
    hd: bool = False
    favorite: bool = False
    blocked: bool = False
    hw_disabled: bool = False
    subscribed: bool = True
    signal_strength: int = 0
    signal_quality: int = 0
    custom: bool = False
    notes: str = ""

    @property
    def sort_key(self) -> tuple:
        parts = re.split(r"[.-]", self.guide_number)
        nums = []
        for p in parts:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(9999)
        return tuple(nums), self.guide_number


@dataclass
class ScanStatus:
    """Current hardware channel scan status from `/lineup_status.json`."""
    scan_in_progress: bool = False
    scan_possible: bool = True
    source: str = "Antenna"
    source_list: List[str] = field(default_factory=lambda: ["Antenna", "Cable"])
    progress: int = 0
    found: int = 0


@dataclass
class TunerTelemetry:
    """Combined HTTP `/status.json` + TCP port 65001 `/tunerN/status` telemetry."""
    tuner_index: int
    resource: str
    in_use: bool = False
    vct_number: str = ""
    vct_name: str = ""
    frequency_hz: int = 0
    lock: str = "none"
    channel_spec: str = "none"
    signal_strength: int = 0       # ss %
    signal_quality: int = 0        # snq %
    symbol_quality: int = 0        # seq %
    network_rate_bps: int = 0      # bps
    packets_per_sec: int = 0       # pps
    target_ip: str = ""
    programs: List[str] = field(default_factory=list)
    raw_status: str = ""

    @property
    def frequency_mhz_str(self) -> str:
        if self.frequency_hz > 0:
            return f"{self.frequency_hz / 1_000_000:.3f} MHz"
        return "—"

    @property
    def bitrate_mbps_str(self) -> str:
        if self.network_rate_bps > 0:
            return f"{self.network_rate_bps / 1_000_000:.2f} Mbps"
        return "0.00 Mbps"


class SignalProbeSession:
    """Maintains an active stream or tuner lock so the user can monitor live RF signal."""

    def __init__(self, device: HDHomeRunDevice) -> None:
        self.device = device
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.active_channel: str = ""
        self.active_tuner: Optional[int] = None
        self.mode: str = "idle"  # idle | stream | rf

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_vchannel_probe(self, guide_number: str, stream_url: str = "") -> None:
        """Open a lightweight HTTP stream reader to hold a tuner lock on `guide_number`."""
        self.stop()
        self._stop_event.clear()
        self.active_channel = guide_number
        self.mode = "stream"
        url = stream_url or f"http://{self.device.local_ip}:5004/auto/v{guide_number}"

        def _worker() -> None:
            try:
                with requests.get(url, stream=True, timeout=(3.0, 3.0)) as resp:
                    for _chunk in resp.iter_content(chunk_size=16384):
                        if self._stop_event.is_set():
                            break
            except Exception:
                pass

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

    def start_rf_channel_probe(self, tuner_index: int, rf_channel: str) -> Optional[str]:
        """Tune a physical RF channel or modulation directly via port 65001 (e.g. `auto:24` or `auto:599000000`)."""
        self.stop()
        self.active_tuner = tuner_index
        self.active_channel = rf_channel
        self.mode = "rf"
        spec = rf_channel if ":" in rf_channel else f"auto:{rf_channel}"
        _, err = getset_var(self.device.local_ip, f"/tuner{tuner_index}/channel", spec)
        return err

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        if self.mode == "rf" and self.active_tuner is not None:
            try:
                getset_var(
                    self.device.local_ip,
                    f"/tuner{self.active_tuner}/channel",
                    "none",
                    timeout=1.0,
                )
            except Exception:
                pass
        self.active_channel = ""
        self.active_tuner = None
        self.mode = "idle"


class HDHomeRunClient:
    """High-level client for HDHomeRun discovery, channel operations, and signal diagnostics."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "HDHomeRun-Linux-Desktop/1.0"})

    def discover_devices(self, extra_ip: Optional[str] = None) -> List[HDHomeRunDevice]:
        """Discover HDHomeRun devices on the LAN via UDP broadcast, cloud API, and manual IP."""
        candidates: Dict[str, Dict[str, Any]] = {}

        # 1. Local UDP broadcast discovery on port 65001
        try:
            for item in udp_discover_devices(timeout=0.9):
                ip = item.get("LocalIP", "")
                if ip:
                    candidates[ip] = item
        except Exception:
            pass

        # 2. SiliconDust HTTP cloud discovery on local subnet
        try:
            resp = self.session.get(DISCOVER_CLOUD_URL, timeout=2.5)
            if resp.ok:
                for item in resp.json():
                    ip = item.get("LocalIP", "")
                    if ip and ip not in candidates:
                        candidates[ip] = item
        except Exception:
            pass

        # 3. Configured manual IP or extra IP
        for raw_ip in [extra_ip, self.config.manual_device_ip]:
            if raw_ip and raw_ip.strip():
                ip = raw_ip.strip().replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
                if ip and ip not in candidates:
                    candidates[ip] = {
                        "DeviceID": ip,
                        "LocalIP": ip,
                        "BaseURL": f"http://{ip}",
                        "DiscoverURL": f"http://{ip}/discover.json",
                        "LineupURL": f"http://{ip}/lineup.json",
                    }

        devices: List[HDHomeRunDevice] = []
        for ip, raw in candidates.items():
            dev = self._enrich_device(ip, raw)
            if dev:
                devices.append(dev)

        return devices

    def _enrich_device(self, ip: str, raw: Dict[str, Any]) -> Optional[HDHomeRunDevice]:
        base_url = raw.get("BaseURL") or f"http://{ip}"
        discover_url = raw.get("DiscoverURL") or f"{base_url}/discover.json"
        lineup_url = raw.get("LineupURL") or f"{base_url}/lineup.json"

        info: Dict[str, Any] = {}
        try:
            resp = self.session.get(discover_url, timeout=2.0)
            if resp.ok:
                info = resp.json()
        except Exception:
            if not raw.get("DeviceID"):
                return None

        source = "Antenna"
        source_list = ["Antenna", "Cable"]
        try:
            st_resp = self.session.get(f"{base_url}/lineup_status.json", timeout=1.5)
            if st_resp.ok:
                st = st_resp.json()
                source = st.get("Source", source)
                source_list = st.get("SourceList", source_list)
        except Exception:
            pass

        device_id = info.get("DeviceID") or raw.get("DeviceID") or ip
        tuner_count = int(info.get("TunerCount") or raw.get("TunerCount") or 2)

        return HDHomeRunDevice(
            device_id=str(device_id),
            local_ip=ip,
            base_url=info.get("BaseURL", base_url),
            discover_url=discover_url,
            lineup_url=info.get("LineupURL", lineup_url),
            friendly_name=info.get("FriendlyName", "HDHomeRun"),
            model_number=info.get("ModelNumber", ""),
            firmware_name=info.get("FirmwareName", ""),
            firmware_version=info.get("FirmwareVersion", ""),
            device_auth=info.get("DeviceAuth", ""),
            tuner_count=tuner_count,
            source=source,
            source_list=source_list,
        )

    def get_lineup(self, device: HDHomeRunDevice, show_all: bool = True) -> List[ChannelItem]:
        """Fetch the channel lineup from the device and merge with local custom/blocked state."""
        url = f"{device.lineup_url}?show=all" if show_all else device.lineup_url
        hw_items: List[Dict[str, Any]] = []
        try:
            resp = self.session.get(url, timeout=4.0)
            if resp.ok:
                hw_items = resp.json()
        except Exception:
            pass

        channels_by_num: Dict[str, ChannelItem] = {}
        for item in hw_items:
            gnum = str(item.get("GuideNumber", "")).strip()
            if not gnum:
                continue
            gname = self.config.channel_name_overrides.get(
                gnum, str(item.get("GuideName", gnum))
            )
            hw_disabled = bool(item.get("Disabled", 0) == 1)
            local_blocked = self.config.is_channel_blocked(gnum)
            hw_fav = bool(item.get("Favorite", 0) == 1)
            local_fav = self.config.is_channel_favorite(gnum)
            subscribed = item.get("Subscribed", 1) != 0
            stream_url = item.get("URL") or f"http://{device.local_ip}:5004/auto/v{gnum}"

            channels_by_num[gnum] = ChannelItem(
                guide_number=gnum,
                guide_name=gname,
                url=stream_url,
                video_codec=str(item.get("VideoCodec", "")),
                audio_codec=str(item.get("AudioCodec", "")),
                hd=bool(item.get("HD", 0) == 1),
                favorite=hw_fav or local_fav,
                blocked=hw_disabled or local_blocked,
                hw_disabled=hw_disabled,
                subscribed=subscribed,
                signal_strength=int(item.get("SignalStrength", 0)),
                signal_quality=int(item.get("SignalQuality", 0)),
                custom=False,
            )

        # Merge custom user-added channels
        for custom in self.config.custom_channels:
            gnum = custom.guide_number
            gname = self.config.channel_name_overrides.get(gnum, custom.guide_name)
            blocked = custom.disabled or self.config.is_channel_blocked(gnum)
            fav = custom.favorite or self.config.is_channel_favorite(gnum)
            url = custom.url or f"http://{device.local_ip}:5004/auto/v{gnum}"
            if gnum in channels_by_num:
                existing = channels_by_num[gnum]
                existing.guide_name = gname
                existing.url = url
                existing.video_codec = custom.video_codec or existing.video_codec
                existing.audio_codec = custom.audio_codec or existing.audio_codec
                existing.hd = custom.hd or existing.hd
                existing.favorite = fav
                existing.blocked = blocked
                existing.custom = True
                existing.notes = custom.notes
            else:
                channels_by_num[gnum] = ChannelItem(
                    guide_number=gnum,
                    guide_name=gname,
                    url=url,
                    video_codec=custom.video_codec,
                    audio_codec=custom.audio_codec,
                    hd=custom.hd,
                    favorite=fav,
                    blocked=blocked,
                    hw_disabled=False,
                    subscribed=True,
                    signal_strength=100,
                    signal_quality=100,
                    custom=True,
                    notes=custom.notes,
                )

        result = list(channels_by_num.values())
        result.sort(key=lambda ch: ch.sort_key)
        return result

    def set_channel_favorite(self, device: HDHomeRunDevice, guide_number: str, favorite: bool) -> bool:
        """Toggle favorite status on both the HDHomeRun hardware and local config."""
        self.config.set_channel_favorite(guide_number, favorite)
        prefix = "v" if favorite else "-v"
        post_url = f"{device.base_url}/lineup.post?favorite={urllib.parse.quote(prefix + guide_number)}"
        try:
            resp = self.session.post(post_url, timeout=2.5)
            return resp.ok
        except Exception:
            return False

    def set_channel_blocked(self, device: HDHomeRunDevice, guide_number: str, blocked: bool) -> bool:
        """Block/disable or unblock a channel on both the HDHomeRun hardware and local config."""
        self.config.set_channel_blocked(guide_number, blocked)
        # On HDHomeRun hardware: `favorite=x<number>` disables the channel; `-x<number>` or `-v<number>` clears disable
        prefix = "x" if blocked else "-x"
        post_url = f"{device.base_url}/lineup.post?favorite={urllib.parse.quote(prefix + guide_number)}"
        try:
            resp = self.session.post(post_url, timeout=2.5)
            return resp.ok
        except Exception:
            return False

    def get_scan_status(self, device: HDHomeRunDevice) -> ScanStatus:
        """Poll `/lineup_status.json` for channel scan status and progress."""
        try:
            resp = self.session.get(f"{device.base_url}/lineup_status.json", timeout=2.5)
            if resp.ok:
                data = resp.json()
                return ScanStatus(
                    scan_in_progress=bool(data.get("ScanInProgress", 0) == 1),
                    scan_possible=bool(data.get("ScanPossible", 1) == 1),
                    source=str(data.get("Source", device.source)),
                    source_list=list(data.get("SourceList", device.source_list)),
                    progress=int(data.get("Progress", 0)),
                    found=int(data.get("Found", 0)),
                )
        except Exception:
            pass
        return ScanStatus()

    def start_channel_scan(self, device: HDHomeRunDevice, source: str = "Antenna") -> Tuple[bool, str]:
        """Start a hardware channel scan on the HDHomeRun."""
        url = f"{device.base_url}/lineup.post?scan=start&source={urllib.parse.quote(source)}"
        try:
            resp = self.session.post(url, timeout=4.0)
            if resp.ok:
                return True, f"Channel scan started ({source})"
            return False, f"HTTP {resp.status_code}: {resp.text}"
        except Exception as exc:
            return False, str(exc)

    def abort_channel_scan(self, device: HDHomeRunDevice) -> Tuple[bool, str]:
        """Stop an in-progress hardware channel scan."""
        url = f"{device.base_url}/lineup.post?scan=abort"
        try:
            resp = self.session.post(url, timeout=3.0)
            if resp.ok:
                return True, "Channel scan stopped"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)

    def get_tuners_telemetry(self, device: HDHomeRunDevice) -> List[TunerTelemetry]:
        """Query both `/status.json` and native port 65001 `/tunerN/status` for all tuners."""
        http_status_by_res: Dict[str, Dict[str, Any]] = {}
        try:
            resp = self.session.get(f"{device.base_url}/status.json", timeout=1.5)
            if resp.ok:
                for entry in resp.json():
                    res = entry.get("Resource", "")
                    if res:
                        http_status_by_res[res] = entry
        except Exception:
            pass

        tuner_count = max(device.tuner_count, len(http_status_by_res), 2)
        telemetry_list: List[TunerTelemetry] = []

        for idx in range(tuner_count):
            res_name = f"tuner{idx}"
            h_stat = http_status_by_res.get(res_name, {})
            ctrl: TunerControlStatus = query_tuner_detailed_status(device.local_ip, idx, timeout=1.0)

            freq = int(h_stat.get("Frequency", 0)) or ctrl.frequency_hz
            ss = int(h_stat.get("SignalStrengthPercent", 0)) or ctrl.signal_strength
            snq = int(h_stat.get("SignalQualityPercent", 0)) or ctrl.signal_to_noise
            seq = int(h_stat.get("SymbolQualityPercent", 0)) or ctrl.symbol_error_quality
            bps = int(h_stat.get("NetworkRate", 0)) or ctrl.bits_per_second
            vct_num = str(h_stat.get("VctNumber", "")) or ctrl.vchannel
            vct_name = str(h_stat.get("VctName", "")) or ctrl.vname
            target_ip = str(h_stat.get("TargetIP", ""))

            in_use = bool(
                vct_num
                or freq > 0
                or (ctrl.channel and ctrl.channel != "none")
                or (ctrl.lock and ctrl.lock != "none")
            )

            telemetry_list.append(
                TunerTelemetry(
                    tuner_index=idx,
                    resource=res_name,
                    in_use=in_use,
                    vct_number=vct_num,
                    vct_name=vct_name,
                    frequency_hz=freq,
                    lock=ctrl.lock,
                    channel_spec=ctrl.channel,
                    signal_strength=ss,
                    signal_quality=snq,
                    symbol_quality=seq,
                    network_rate_bps=bps,
                    packets_per_sec=ctrl.packets_per_second,
                    target_ip=target_ip,
                    programs=ctrl.programs,
                    raw_status=ctrl.raw_status,
                )
            )

        return telemetry_list

    def quick_probe_channel_signal(
        self,
        device: HDHomeRunDevice,
        guide_number: str,
        dwell_seconds: float = 1.6,
    ) -> Optional[TunerTelemetry]:
        """Briefly open a channel stream for `dwell_seconds` and measure live RF signal."""
        probe = SignalProbeSession(device)
        try:
            probe.start_vchannel_probe(guide_number)
            deadline = time.time() + dwell_seconds
            best: Optional[TunerTelemetry] = None
            while time.time() < deadline:
                time.sleep(0.4)
                tuners = self.get_tuners_telemetry(device)
                for t in tuners:
                    if t.vct_number == guide_number or (t.in_use and best is None):
                        best = t
                        if t.signal_strength > 0 and t.symbol_quality > 0:
                            break
            return best
        finally:
            probe.stop()

