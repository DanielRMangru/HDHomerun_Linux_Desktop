"""Electronic Program Guide (EPG) provider supporting SiliconDust Guide API and XMLTV."""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..config import AppConfig
from .client import HDHomeRunDevice


SILICONDUST_GUIDE_URL = "https://api.hdhomerun.com/api/guide.php"


@dataclass
class ProgramEntry:
    """Single TV program airing on a channel."""
    guide_number: str
    channel_name: str
    title: str
    start_time: int          # Unix timestamp (seconds)
    end_time: int            # Unix timestamp (seconds)
    episode_title: str = ""
    episode_number: str = ""
    synopsis: str = ""
    image_url: str = ""
    series_id: str = ""
    original_airdate: int = 0
    categories: List[str] = field(default_factory=list)
    is_new: bool = False

    @property
    def duration_minutes(self) -> int:
        return max(1, int(round((self.end_time - self.start_time) / 60.0)))

    def is_airing_at(self, ts: Optional[float] = None) -> bool:
        now = ts if ts is not None else time.time()
        return self.start_time <= now < self.end_time

    def progress_fraction(self, ts: Optional[float] = None) -> float:
        now = ts if ts is not None else time.time()
        total = max(1, self.end_time - self.start_time)
        return max(0.0, min(1.0, (now - self.start_time) / total))

    @property
    def time_range_str(self) -> str:
        st = datetime.datetime.fromtimestamp(self.start_time).strftime("%-I:%M %p")
        et = datetime.datetime.fromtimestamp(self.end_time).strftime("%-I:%M %p")
        return f"{st} – {et}"


@dataclass
class ChannelGuideData:
    """Channel metadata and sorted list of upcoming programs."""
    guide_number: str
    guide_name: str
    affiliate: str = ""
    image_url: str = ""
    programs: List[ProgramEntry] = field(default_factory=list)


def _parse_xmltv_timestamp(ts_str: str) -> Optional[int]:
    """Parse XMLTV timestamp format `YYYYMMDDHHMMSS +0000` into Unix epoch seconds."""
    ts_str = ts_str.strip()
    if not ts_str:
        return None
    m = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?", ts_str)
    if not m:
        m2 = re.match(r"^(\d{12})(?:\s*([+-]\d{4}))?", ts_str)
        if not m2:
            return None
        dt_part = m2.group(1) + "00"
        tz_part = m2.group(2)
    else:
        dt_part = m.group(1)
        tz_part = m.group(2)

    try:
        if tz_part:
            dt = datetime.datetime.strptime(f"{dt_part} {tz_part}", "%Y%m%d%H%M%S %z")
        else:
            dt = datetime.datetime.strptime(dt_part, "%Y%m%d%H%M%S")
        return int(dt.timestamp())
    except ValueError:
        return None


class EPGManager:
    """Fetches, caches, and queries EPG data from SiliconDust Cloud API and/or XMLTV."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self.channels: Dict[str, ChannelGuideData] = {}
        self.last_updated: float = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "HDHomeRun-Linux-Desktop/1.0"})
        self._load_cache()

    def _load_cache(self) -> None:
        cache_path = self.config.epg_cache_file
        if not cache_path.exists():
            return
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            self.last_updated = float(raw.get("last_updated", 0.0))
            loaded: Dict[str, ChannelGuideData] = {}
            now_cutoff = int(time.time()) - 7200
            for gnum, ch_data in raw.get("channels", {}).items():
                progs = [
                    ProgramEntry(**p)
                    for p in ch_data.get("programs", [])
                    if int(p.get("end_time", 0)) >= now_cutoff
                ]
                progs.sort(key=lambda p: p.start_time)
                loaded[gnum] = ChannelGuideData(
                    guide_number=gnum,
                    guide_name=ch_data.get("guide_name", gnum),
                    affiliate=ch_data.get("affiliate", ""),
                    image_url=ch_data.get("image_url", ""),
                    programs=progs,
                )
            with self._lock:
                self.channels = loaded
        except Exception:
            pass

    def _save_cache(self) -> None:
        with self._lock:
            serializable = {
                "last_updated": self.last_updated,
                "channels": {
                    gnum: {
                        "guide_number": ch.guide_number,
                        "guide_name": ch.guide_name,
                        "affiliate": ch.affiliate,
                        "image_url": ch.image_url,
                        "programs": [asdict(p) for p in ch.programs],
                    }
                    for gnum, ch in self.channels.items()
                },
            }
        try:
            tmp = self.config.epg_cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(serializable), encoding="utf-8")
            os.replace(tmp, self.config.epg_cache_file)
        except Exception:
            pass

    def refresh_epg(self, device: Optional[HDHomeRunDevice] = None, hours_ahead: Optional[int] = None) -> Tuple[bool, str]:
        """Fetch guide data from configured sources (SiliconDust API and/or XMLTV)."""
        target_hours = hours_ahead or self.config.epg_hours_ahead or 12
        source_mode = self.config.epg_source
        merged: Dict[str, ChannelGuideData] = {}
        messages: List[str] = []

        if source_mode in ("silicondust", "both") and device and device.device_auth:
            ok, msg = self._fetch_silicondust_epg(device, target_hours, merged)
            messages.append(msg)

        if source_mode in ("xmltv", "both") or (not merged and self.config.xmltv_path_or_url):
            if self.config.xmltv_path_or_url:
                ok_xml, msg_xml = self._fetch_xmltv_epg(self.config.xmltv_path_or_url, merged)
                messages.append(msg_xml)

        if merged:
            with self._lock:
                self.channels = merged
                self.last_updated = time.time()
            self._save_cache()
            total_progs = sum(len(ch.programs) for ch in merged.values())
            return True, f"Loaded {total_progs} programs across {len(merged)} channels."

        return False, "; ".join(messages) if messages else "No EPG source available."

    def _fetch_silicondust_epg(
        self,
        device: HDHomeRunDevice,
        hours_ahead: int,
        dest: Dict[str, ChannelGuideData],
    ) -> Tuple[bool, str]:
        """Fetch EPG slices from SiliconDust `https://api.hdhomerun.com/api/guide.php`."""
        auth = device.device_auth
        if not auth:
            return False, "Device does not report a DeviceAuth token."

        now_ts = int(time.time())
        end_goal_ts = now_ts + (hours_ahead * 3600)
        cursor_ts: Optional[int] = None
        seen_programs: Dict[Tuple[str, int], ProgramEntry] = {}

        # Each SiliconDust guide call returns ~4 hours; step forward up to 4 slices
        max_slices = max(1, min(6, (hours_ahead + 3) // 4))
        for _ in range(max_slices):
            params: Dict[str, Any] = {"DeviceAuth": auth}
            if cursor_ts is not None:
                params["Start"] = cursor_ts
            try:
                resp = self.session.get(
                    SILICONDUST_GUIDE_URL,
                    params=params,
                    headers={"X-HDHomeRun-Auth": auth},
                    timeout=8.0,
                )
                if not resp.ok:
                    break
                payload = resp.json()
            except Exception as exc:
                if not dest:
                    return False, f"SiliconDust EPG request failed: {exc}"
                break

            if not isinstance(payload, list) or not payload:
                break

            max_end_seen = 0
            for ch_item in payload:
                gnum = str(ch_item.get("GuideNumber", "")).strip()
                if not gnum:
                    continue
                gname = str(ch_item.get("GuideName", gnum))
                affiliate = str(ch_item.get("Affiliate", ""))
                img_url = str(ch_item.get("ImageURL", ""))

                if gnum not in dest:
                    dest[gnum] = ChannelGuideData(
                        guide_number=gnum,
                        guide_name=gname,
                        affiliate=affiliate,
                        image_url=img_url,
                        programs=[],
                    )

                for raw_prog in ch_item.get("Guide", []):
                    st = int(raw_prog.get("StartTime", 0))
                    et = int(raw_prog.get("EndTime", 0))
                    if not st or not et:
                        continue
                    if et > max_end_seen:
                        max_end_seen = et
                    key = (gnum, st)
                    if key in seen_programs:
                        continue
                    filters = raw_prog.get("Filter") or []
                    if isinstance(filters, str):
                        filters = [filters]
                    prog = ProgramEntry(
                        guide_number=gnum,
                        channel_name=gname,
                        title=str(raw_prog.get("Title", "Untitled Program")),
                        start_time=st,
                        end_time=et,
                        episode_title=str(raw_prog.get("EpisodeTitle", "")),
                        episode_number=str(raw_prog.get("EpisodeNumber", "")),
                        synopsis=str(raw_prog.get("Synopsis", "")),
                        image_url=str(raw_prog.get("ImageURL", "")),
                        series_id=str(raw_prog.get("SeriesID", "")),
                        original_airdate=int(raw_prog.get("OriginalAirdate", 0)),
                        categories=[str(f) for f in filters],
                        is_new=bool(raw_prog.get("First", 0) == 1),
                    )
                    seen_programs[key] = prog
                    dest[gnum].programs.append(prog)

            if max_end_seen == 0 or max_end_seen >= end_goal_ts:
                break
            if cursor_ts is not None and max_end_seen <= cursor_ts:
                break
            cursor_ts = max_end_seen

        for ch_data in dest.values():
            ch_data.programs.sort(key=lambda p: p.start_time)

        return True, f"Fetched SiliconDust EPG for {len(dest)} channels."

    def _fetch_xmltv_epg(self, path_or_url: str, dest: Dict[str, ChannelGuideData]) -> Tuple[bool, str]:
        """Parse an XMLTV file or URL into `dest`."""
        try:
            if path_or_url.startswith(("http://", "https://")):
                resp = self.session.get(path_or_url, timeout=12.0)
                resp.raise_for_status()
                xml_bytes = resp.content
            else:
                xml_bytes = Path(path_or_url).expanduser().read_bytes()

            root = ET.fromstring(xml_bytes)
        except Exception as exc:
            return False, f"XMLTV load error: {exc}"

        xml_id_to_gnum: Dict[str, Tuple[str, str]] = {}
        for ch_elem in root.findall("channel"):
            cid = ch_elem.get("id", "").strip()
            disp_elems = [
                (el.text or "").strip()
                for el in ch_elem.findall("display-name")
                if (el.text or "").strip()
            ]
            gnum = cid
            gname = disp_elems[0] if disp_elems else cid
            for d in disp_elems:
                if re.match(r"^\d+(\.\d+)?$", d):
                    gnum = d
                    break
            xml_id_to_gnum[cid] = (gnum, gname)
            if gnum not in dest:
                dest[gnum] = ChannelGuideData(guide_number=gnum, guide_name=gname)

        count = 0
        for prog_elem in root.findall("programme"):
            cid = prog_elem.get("channel", "").strip()
            st = _parse_xmltv_timestamp(prog_elem.get("start", ""))
            et = _parse_xmltv_timestamp(prog_elem.get("stop", ""))
            if not st or not et:
                continue
            gnum, gname = xml_id_to_gnum.get(cid, (cid, cid))
            if gnum not in dest:
                dest[gnum] = ChannelGuideData(guide_number=gnum, guide_name=gname)

            title = (prog_elem.findtext("title") or "Program").strip()
            sub_title = (prog_elem.findtext("sub-title") or "").strip()
            desc = (prog_elem.findtext("desc") or "").strip()
            cats = [
                (c.text or "").strip()
                for c in prog_elem.findall("category")
                if (c.text or "").strip()
            ]
            ep_num = (prog_elem.findtext("episode-num") or "").strip()

            dest[gnum].programs.append(
                ProgramEntry(
                    guide_number=gnum,
                    channel_name=gname,
                    title=title,
                    start_time=st,
                    end_time=et,
                    episode_title=sub_title,
                    episode_number=ep_num,
                    synopsis=desc,
                    categories=cats,
                )
            )
            count += 1

        for ch_data in dest.values():
            ch_data.programs.sort(key=lambda p: p.start_time)

        return True, f"Loaded {count} programs from XMLTV."

    def get_now_and_next(
        self, guide_number: str, now_ts: Optional[float] = None
    ) -> Tuple[Optional[ProgramEntry], Optional[ProgramEntry]]:
        """Return (current_program, next_program) for a channel."""
        now = now_ts if now_ts is not None else time.time()
        with self._lock:
            ch = self.channels.get(guide_number)
            if not ch:
                return None, None
            current_prog: Optional[ProgramEntry] = None
            next_prog: Optional[ProgramEntry] = None
            for p in ch.programs:
                if p.start_time <= now < p.end_time:
                    current_prog = p
                elif p.start_time > now:
                    next_prog = p
                    break
            return current_prog, next_prog

    def get_programs_in_window(
        self, guide_number: str, start_ts: float, end_ts: float
    ) -> List[ProgramEntry]:
        with self._lock:
            ch = self.channels.get(guide_number)
            if not ch:
                return []
            return [
                p
                for p in ch.programs
                if p.end_time > start_ts and p.start_time < end_ts
            ]

    def search_programs(self, query: str = "", category: str = "") -> List[ProgramEntry]:
        q = query.strip().lower()
        cat = category.strip().lower()
        now = time.time()
        matches: List[ProgramEntry] = []
        with self._lock:
            for ch in self.channels.values():
                for p in ch.programs:
                    if p.end_time < now:
                        continue
                    if cat and cat != "all":
                        if not any(cat in c.lower() for c in p.categories):
                            continue
                    if q:
                        haystack = f"{p.title} {p.episode_title} {p.synopsis} {p.channel_name} {p.guide_number}".lower()
                        if q not in haystack:
                            continue
                    matches.append(p)
        matches.sort(key=lambda p: (p.start_time, p.guide_number))
        return matches

