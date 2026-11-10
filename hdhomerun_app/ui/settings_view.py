"""Settings & Hardware Device Information View."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

from ..backend.client import HDHomeRunDevice
from ..config import AppConfig


class SettingsView(Gtk.ScrolledWindow):
    """Configure HDHomeRun discovery, DVR storage directory, EPG sources, and playback."""

    def __init__(
        self,
        config: AppConfig,
        on_rediscover: Callable[[], None],
        on_settings_saved: Callable[[], None],
        on_toast: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.set_vexpand(True)
        self.set_hexpand(True)

        self.config = config
        self.on_rediscover = on_rediscover
        self.on_settings_saved = on_settings_saved
        self.on_toast = on_toast
        self.device: Optional[HDHomeRunDevice] = None

        self._build_ui()

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(24)
        outer.set_margin_end(24)

        # --- 1. Hardware Device Details Card ---
        dev_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        dev_card.add_css_class("card-panel")

        dev_title = Gtk.Label(label="HDHomeRun Hardware Device", halign=Gtk.Align.START)
        dev_title.add_css_class("title-4")
        dev_card.append(dev_title)

        self.lbl_device_info = Gtk.Label(
            label="Searching for HDHomeRun devices on local network...",
            halign=Gtk.Align.START,
        )
        self.lbl_device_info.set_selectable(True)
        self.lbl_device_info.add_css_class("mono-value")
        dev_card.append(self.lbl_device_info)

        ip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ip_row.append(Gtk.Label(label="Manual Device IP (optional):"))
        self.entry_manual_ip = Gtk.Entry(
            text=self.config.manual_device_ip,
            placeholder_text="e.g. 192.168.1.100",
            hexpand=True,
        )
        ip_row.append(self.entry_manual_ip)

        btn_discover = Gtk.Button(label="Re-Discover Network Devices")
        btn_discover.set_icon_name("view-refresh-symbolic")
        btn_discover.connect("clicked", self._on_discover_clicked)
        ip_row.append(btn_discover)
        dev_card.append(ip_row)

        outer.append(dev_card)

        # --- 2. DVR & Local Storage Card ---
        dvr_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dvr_card.add_css_class("card-panel")

        dvr_title = Gtk.Label(label="Local DVR & Recording Options", halign=Gtk.Align.START)
        dvr_title.add_css_class("title-4")
        dvr_card.append(dvr_title)

        grid_dvr = Gtk.Grid(column_spacing=12, row_spacing=10)

        grid_dvr.attach(
            Gtk.Label(label="Recordings Directory:", halign=Gtk.Align.START), 0, 0, 1, 1
        )
        self.entry_rec_dir = Gtk.Entry(text=self.config.recordings_dir, hexpand=True)
        grid_dvr.attach(self.entry_rec_dir, 1, 0, 1, 1)

        grid_dvr.attach(
            Gtk.Label(label="Use ffmpeg Remuxing:", halign=Gtk.Align.START), 0, 1, 1, 1
        )
        self.chk_ffmpeg = Gtk.CheckButton(
            label="Use ffmpeg (-c copy) instead of direct HTTP MPEG-TS capture",
            active=self.config.use_ffmpeg_for_recording,
        )
        grid_dvr.attach(self.chk_ffmpeg, 1, 1, 1, 1)

        grid_dvr.attach(
            Gtk.Label(label="Container Format:", halign=Gtk.Align.START), 0, 2, 1, 1
        )
        self.combo_format = Gtk.DropDown.new_from_strings(["ts", "mp4"])
        self.combo_format.set_selected(1 if self.config.recording_format == "mp4" else 0)
        grid_dvr.attach(self.combo_format, 1, 2, 1, 1)

        grid_dvr.attach(
            Gtk.Label(label="Pre-Roll Padding (seconds):", halign=Gtk.Align.START),
            0,
            3,
            1,
            1,
        )
        self.spin_pre = Gtk.SpinButton.new_with_range(0, 600, 15)
        self.spin_pre.set_value(self.config.default_pre_padding_sec)
        grid_dvr.attach(self.spin_pre, 1, 3, 1, 1)

        grid_dvr.attach(
            Gtk.Label(label="Post-Roll Padding (seconds):", halign=Gtk.Align.START),
            0,
            4,
            1,
            1,
        )
        self.spin_post = Gtk.SpinButton.new_with_range(0, 900, 15)
        self.spin_post.set_value(self.config.default_post_padding_sec)
        grid_dvr.attach(self.spin_post, 1, 4, 1, 1)

        dvr_card.append(grid_dvr)
        outer.append(dvr_card)

        # --- 3. Program Guide (EPG) & Playback Card ---
        epg_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        epg_card.add_css_class("card-panel")

        epg_title = Gtk.Label(
            label="Electronic Program Guide (EPG) & Playback", halign=Gtk.Align.START
        )
        epg_title.add_css_class("title-4")
        epg_card.append(epg_title)

        grid_epg = Gtk.Grid(column_spacing=12, row_spacing=10)

        grid_epg.attach(
            Gtk.Label(label="EPG Data Source:", halign=Gtk.Align.START), 0, 0, 1, 1
        )
        sources = ["silicondust", "xmltv", "both"]
        self.combo_epg_src = Gtk.DropDown.new_from_strings(sources)
        if self.config.epg_source in sources:
            self.combo_epg_src.set_selected(sources.index(self.config.epg_source))
        grid_epg.attach(self.combo_epg_src, 1, 0, 1, 1)

        grid_epg.attach(
            Gtk.Label(label="Custom XMLTV File or URL:", halign=Gtk.Align.START),
            0,
            1,
            1,
            1,
        )
        self.entry_xmltv = Gtk.Entry(
            text=self.config.xmltv_path_or_url,
            placeholder_text="/path/to/xmltv.xml or https://...",
            hexpand=True,
        )
        grid_epg.attach(self.entry_xmltv, 1, 1, 1, 1)

        grid_epg.attach(
            Gtk.Label(label="External Media Player:", halign=Gtk.Align.START), 0, 2, 1, 1
        )
        players = ["vlc", "mpv", "ffplay"]
        self.combo_player = Gtk.DropDown.new_from_strings(players)
        if self.config.external_player in players:
            self.combo_player.set_selected(players.index(self.config.external_player))
        grid_epg.attach(self.combo_player, 1, 2, 1, 1)

        self.chk_show_blocked = Gtk.CheckButton(
            label="Show blocked/disabled channels in Live TV and EPG",
            active=self.config.show_blocked_channels,
        )
        grid_epg.attach(self.chk_show_blocked, 0, 3, 2, 1)

        self.chk_show_unsub = Gtk.CheckButton(
            label="Show unsubscribed / no-signal channels in views",
            active=self.config.show_unsubscribed_channels,
        )
        grid_epg.attach(self.chk_show_unsub, 0, 4, 2, 1)

        epg_card.append(grid_epg)
        outer.append(epg_card)

        # --- Save Button ---
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.END)
        btn_save = Gtk.Button(label="Save Settings")
        btn_save.add_css_class("suggested-action")
        btn_save.connect("clicked", self._on_save_clicked)
        btn_row.append(btn_save)
        outer.append(btn_row)

        self.set_child(outer)

    def set_device(self, device: Optional[HDHomeRunDevice]) -> None:
        self.device = device
        if not device:
            self.lbl_device_info.set_text("No HDHomeRun device currently connected.")
            return
        info_lines = [
            f"Friendly Name : {device.friendly_name}",
            f"Model Number  : {device.model_number}",
            f"Device ID     : {device.device_id}",
            f"Local IP      : {device.local_ip} ({device.base_url})",
            f"Firmware      : {device.firmware_name} ({device.firmware_version})",
            f"Tuners        : {device.tuner_count}  |  Signal Source: {device.source}",
        ]
        self.lbl_device_info.set_text("\n".join(info_lines))

    def _on_discover_clicked(self, _btn: Gtk.Button) -> None:
        self.config.manual_device_ip = self.entry_manual_ip.get_text().strip()
        self.config.save()
        self.on_rediscover()

    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        self.config.manual_device_ip = self.entry_manual_ip.get_text().strip()
        self.config.recordings_dir = self.entry_rec_dir.get_text().strip() or self.config.recordings_dir
        self.config.use_ffmpeg_for_recording = self.chk_ffmpeg.get_active()

        fmt_item = self.combo_format.get_selected_item()
        if fmt_item:
            self.config.recording_format = fmt_item.get_string()

        self.config.default_pre_padding_sec = int(self.spin_pre.get_value())
        self.config.default_post_padding_sec = int(self.spin_post.get_value())

        src_item = self.combo_epg_src.get_selected_item()
        if src_item:
            self.config.epg_source = src_item.get_string()

        self.config.xmltv_path_or_url = self.entry_xmltv.get_text().strip()

        pl_item = self.combo_player.get_selected_item()
        if pl_item:
            self.config.external_player = pl_item.get_string()

        self.config.show_blocked_channels = self.chk_show_blocked.get_active()
        self.config.show_unsubscribed_channels = self.chk_show_unsub.get_active()

        self.config.save()
        self.on_toast("Settings saved.")
        self.on_settings_saved()

