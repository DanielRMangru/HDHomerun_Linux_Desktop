"""Persistent configuration and local state management for HDHomeRun Desktop."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "hdhomerun-desktop"
DEFAULT_RECORDINGS_DIR = Path.home() / "Videos" / "HDHomeRun"


@dataclass
class CustomChannel:
    """User-defined custom channel or override."""
    guide_number: str
    guide_name: str
    url: str
    video_codec: str = "MPEG2"
    audio_codec: str = "AC3"
    hd: bool = True
    favorite: bool = False
    disabled: bool = False
    notes: str = ""

    def to_lineup_dict(self) -> Dict[str, Any]:
        return {
            "GuideNumber": self.guide_number,
            "GuideName": self.guide_name,
            "URL": self.url,
            "VideoCodec": self.video_codec,
            "AudioCodec": self.audio_codec,
            "HD": 1 if self.hd else 0,
            "Favorite": 1 if self.favorite else 0,
            "Disabled": 1 if self.disabled else 0,
            "Custom": True,
            "Notes": self.notes,
        }


@dataclass
class ScheduledRecording:
    """A scheduled or active or completed DVR recording job."""
    id: str
    title: str
    channel_number: str
    channel_name: str
    stream_url: str
    start_time: float  # Unix timestamp
    end_time: float    # Unix timestamp
    pre_padding_sec: int = 30
    post_padding_sec: int = 60
    episode_title: str = ""
    episode_number: str = ""
    synopsis: str = ""
    status: str = "scheduled"  # scheduled | recording | completed | failed | cancelled
    output_path: str = ""
    bytes_written: int = 0
    error_message: str = ""
    created_at: float = 0.0

    @property
    def effective_start(self) -> float:
        return self.start_time - self.pre_padding_sec

    @property
    def effective_end(self) -> float:
        return self.end_time + self.post_padding_sec

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_time - self.start_time)


class AppConfig:
    """Thread-safe configuration store backed by JSON on disk."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self.epg_cache_file = self.config_dir / "epg_cache.json"

        # Settings defaults
        self.selected_device_id: str = ""
        self.manual_device_ip: str = ""
        self.recordings_dir: str = str(DEFAULT_RECORDINGS_DIR)
        self.recording_format: str = "ts"  # "ts" (direct MPEG-TS) or "mp4" (ffmpeg copy)
        self.use_ffmpeg_for_recording: bool = False
        self.external_player: str = "vlc"  # "vlc", "mpv", "ffplay"
        self.epg_source: str = "silicondust"  # "silicondust", "xmltv", "both"
        self.xmltv_path_or_url: str = ""
        self.epg_hours_ahead: int = 12
        self.show_unsubscribed_channels: bool = False
        self.show_blocked_channels: bool = False
        self.default_pre_padding_sec: int = 30
        self.default_post_padding_sec: int = 60

        # Channel overrides & additions
        self.blocked_channels: List[str] = []     # GuideNumbers blocked locally
        self.favorite_channels: List[str] = []    # GuideNumbers favorited locally
        self.channel_name_overrides: Dict[str, str] = {}  # GuideNumber -> custom name
        self.custom_channels: List[CustomChannel] = []

        # DVR Recordings
        self.recordings: List[ScheduledRecording] = []

        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.config_file.exists():
                self._ensure_recordings_dir()
                return
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception:
                return

            self.selected_device_id = data.get("selected_device_id", self.selected_device_id)
            self.manual_device_ip = data.get("manual_device_ip", self.manual_device_ip)
            self.recordings_dir = data.get("recordings_dir", self.recordings_dir)
            self.recording_format = data.get("recording_format", self.recording_format)
            self.use_ffmpeg_for_recording = bool(data.get("use_ffmpeg_for_recording", False))
            self.external_player = data.get("external_player", self.external_player)
            self.epg_source = data.get("epg_source", self.epg_source)
            self.xmltv_path_or_url = data.get("xmltv_path_or_url", self.xmltv_path_or_url)
            self.epg_hours_ahead = int(data.get("epg_hours_ahead", self.epg_hours_ahead))
            self.show_unsubscribed_channels = bool(data.get("show_unsubscribed_channels", False))
            self.show_blocked_channels = bool(data.get("show_blocked_channels", False))
            self.default_pre_padding_sec = int(data.get("default_pre_padding_sec", 30))
            self.default_post_padding_sec = int(data.get("default_post_padding_sec", 60))

            self.blocked_channels = list(data.get("blocked_channels", []))
            self.favorite_channels = list(data.get("favorite_channels", []))
            self.channel_name_overrides = dict(data.get("channel_name_overrides", {}))

            self.custom_channels = [
                CustomChannel(**ch) for ch in data.get("custom_channels", [])
            ]
            self.recordings = [
                ScheduledRecording(**rec) for rec in data.get("recordings", [])
            ]
            self._ensure_recordings_dir()

    def save(self) -> None:
        with self._lock:
            self._ensure_recordings_dir()
            data = {
                "selected_device_id": self.selected_device_id,
                "manual_device_ip": self.manual_device_ip,
                "recordings_dir": self.recordings_dir,
                "recording_format": self.recording_format,
                "use_ffmpeg_for_recording": self.use_ffmpeg_for_recording,
                "external_player": self.external_player,
                "epg_source": self.epg_source,
                "xmltv_path_or_url": self.xmltv_path_or_url,
                "epg_hours_ahead": self.epg_hours_ahead,
                "show_unsubscribed_channels": self.show_unsubscribed_channels,
                "show_blocked_channels": self.show_blocked_channels,
                "default_pre_padding_sec": self.default_pre_padding_sec,
                "default_post_padding_sec": self.default_post_padding_sec,
                "blocked_channels": self.blocked_channels,
                "favorite_channels": self.favorite_channels,
                "channel_name_overrides": self.channel_name_overrides,
                "custom_channels": [asdict(ch) for ch in self.custom_channels],
                "recordings": [asdict(rec) for rec in self.recordings],
            }
            tmp_file = self.config_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp_file, self.config_file)

    def _ensure_recordings_dir(self) -> None:
        try:
            Path(self.recordings_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def is_channel_blocked(self, guide_number: str) -> bool:
        with self._lock:
            return guide_number in self.blocked_channels

    def set_channel_blocked(self, guide_number: str, blocked: bool) -> None:
        with self._lock:
            if blocked and guide_number not in self.blocked_channels:
                self.blocked_channels.append(guide_number)
            elif not blocked and guide_number in self.blocked_channels:
                self.blocked_channels.remove(guide_number)
            for ch in self.custom_channels:
                if ch.guide_number == guide_number:
                    ch.disabled = blocked
            self.save()

    def is_channel_favorite(self, guide_number: str) -> bool:
        with self._lock:
            return guide_number in self.favorite_channels

    def set_channel_favorite(self, guide_number: str, favorite: bool) -> None:
        with self._lock:
            if favorite and guide_number not in self.favorite_channels:
                self.favorite_channels.append(guide_number)
            elif not favorite and guide_number in self.favorite_channels:
                self.favorite_channels.remove(guide_number)
            for ch in self.custom_channels:
                if ch.guide_number == guide_number:
                    ch.favorite = favorite
            self.save()

    def add_or_update_custom_channel(self, channel: CustomChannel) -> None:
        with self._lock:
            for idx, existing in enumerate(self.custom_channels):
                if existing.guide_number == channel.guide_number:
                    self.custom_channels[idx] = channel
                    self.save()
                    return
            self.custom_channels.append(channel)
            self.save()

    def remove_custom_channel(self, guide_number: str) -> bool:
        with self._lock:
            before = len(self.custom_channels)
            self.custom_channels = [
                ch for ch in self.custom_channels if ch.guide_number != guide_number
            ]
            if len(self.custom_channels) != before:
                self.save()
                return True
            return False

