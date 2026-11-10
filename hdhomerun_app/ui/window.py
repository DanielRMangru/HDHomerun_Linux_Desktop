"""Main Adw.ApplicationWindow coordinating views, device discovery, EPG, and DVR."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from ..backend.client import ChannelItem, HDHomeRunClient, HDHomeRunDevice
from ..backend.epg import EPGManager
from ..backend.recorder import DVRManager
from ..config import AppConfig
from .channels_view import ChannelsView
from .dvr_view import DVRView
from .guide_view import GuideView
from .live_view import LiveTVView
from .settings_view import SettingsView
from .signal_view import SignalView


class MainWindow(Adw.ApplicationWindow):
    """Primary desktop window for HDHomeRun Ubuntu Desktop."""

    def __init__(self, app: Adw.Application, config: AppConfig) -> None:
        super().__init__(application=app, title="HDHomeRun Desktop")
        self.set_default_size(1260, 800)

        self.config = config
        self.client = HDHomeRunClient(config)
        self.epg = EPGManager(config)
        self.dvr = DVRManager(config)
        self.dvr.start_scheduler()

        self.devices: List[HDHomeRunDevice] = []
        self.current_device: Optional[HDHomeRunDevice] = None
        self.channels: List[ChannelItem] = []

        self._load_css()
        self._build_ui()
        self.connect("close-request", self._on_close_request)

        # Start initial network discovery asynchronously
        GLib.idle_add(self.rediscover_devices)

    def _load_css(self) -> None:
        css_file = Path(__file__).parent / "style.css"
        if css_file.exists():
            provider = Gtk.CssProvider()
            provider.load_from_path(str(css_file))
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.toast_overlay.set_child(root_box)

        # --- HeaderBar with Device Selector & ViewSwitcher ---
        header = Adw.HeaderBar()

        # Left side of HeaderBar: Device dropdown & refresh button
        dev_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.device_combo = Gtk.DropDown.new_from_strings(["Discovering HDHomeRun..."])
        self.device_combo.set_size_request(250, -1)
        self.device_combo.connect("notify::selected", self._on_device_combo_changed)
        dev_box.append(self.device_combo)

        btn_refresh_dev = Gtk.Button()
        btn_refresh_dev.set_icon_name("view-refresh-symbolic")
        btn_refresh_dev.set_tooltip_text("Refresh HDHomeRun Device & Lineup")
        btn_refresh_dev.connect("clicked", lambda *_: self.rediscover_devices())
        dev_box.append(btn_refresh_dev)

        header.pack_start(dev_box)

        # Center of HeaderBar: ViewStack & ViewSwitcher
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        # Right side of HeaderBar: Global Fullscreen toggle button
        self.btn_window_fullscreen = Gtk.Button()
        self.btn_window_fullscreen.set_icon_name("view-fullscreen-symbolic")
        self.btn_window_fullscreen.set_tooltip_text("Toggle Fullscreen (F11)")
        self.btn_window_fullscreen.connect("clicked", lambda *_: self.toggle_fullscreen())
        header.pack_end(self.btn_window_fullscreen)

        # Add F11 key shortcut controller
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        root_box.append(header)

        # --- Instantiate Views ---
        self.live_view = LiveTVView(
            config=self.config,
            client=self.client,
            epg=self.epg,
            dvr=self.dvr,
            on_toast=self.show_toast,
            on_inspect_signal=self.switch_to_signal_probe,
        )
        self.guide_view = GuideView(
            config=self.config,
            epg=self.epg,
            dvr=self.dvr,
            on_watch_channel=self.switch_to_live_channel,
            on_toast=self.show_toast,
        )
        self.channels_view = ChannelsView(
            config=self.config,
            client=self.client,
            on_channels_changed=self.reload_lineup,
            on_watch_channel=self.switch_to_live_channel,
            on_probe_signal=self.switch_to_signal_probe,
            on_toast=self.show_toast,
        )
        self.signal_view = SignalView(
            config=self.config,
            client=self.client,
            on_toast=self.show_toast,
        )
        self.dvr_view = DVRView(
            config=self.config,
            dvr=self.dvr,
            on_toast=self.show_toast,
        )
        self.settings_view = SettingsView(
            config=self.config,
            on_rediscover=self.rediscover_devices,
            on_settings_saved=self._propagate_state_to_views,
            on_toast=self.show_toast,
        )

        self.view_stack.add_titled_with_icon(
            self.live_view, "live", "Live TV", "video-display-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            self.guide_view, "guide", "Program Guide", "view-grid-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            self.channels_view, "channels", "Channels & Scan", "view-list-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            self.signal_view,
            "signal",
            "Signal Meter",
            "network-cellular-signal-excellent-symbolic",
        )
        self.view_stack.add_titled_with_icon(
            self.dvr_view, "dvr", "DVR Recordings", "media-record-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            self.settings_view, "settings", "Settings", "emblem-system-symbolic"
        )

        root_box.append(self.view_stack)

    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)

    def switch_to_live_channel(self, channel: ChannelItem) -> None:
        self.view_stack.set_visible_child_name("live")
        self.live_view.play_channel(channel)

    def switch_to_signal_probe(self, guide_number: str) -> None:
        self.live_view.stop_playback()
        self.view_stack.set_visible_child_name("signal")
        if guide_number:
            self.signal_view.probe_specific_channel(guide_number)

    def rediscover_devices(self) -> None:
        def _worker() -> None:
            devs = self.client.discover_devices()
            GLib.idle_add(self._on_devices_discovered, devs)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_devices_discovered(self, devs: List[HDHomeRunDevice]) -> None:
        self.devices = devs
        if not devs:
            self.current_device = None
            self.device_combo.set_model(Gtk.StringList.new(["No HDHomeRun Found"]))
            self.show_toast("No HDHomeRun device discovered on the network. Check Settings.")
            self._propagate_state_to_views()
            return

        titles = [d.display_title for d in devs]
        self.device_combo.set_model(Gtk.StringList.new(titles))

        selected_idx = 0
        if self.config.selected_device_id:
            for idx, d in enumerate(devs):
                if d.device_id == self.config.selected_device_id:
                    selected_idx = idx
                    break

        self.device_combo.set_selected(selected_idx)
        self.current_device = devs[selected_idx]
        self.config.selected_device_id = self.current_device.device_id
        self.config.save()

        self.reload_lineup(auto_refresh_epg=True)

    def _on_device_combo_changed(self, combo: Gtk.DropDown, _pspec: object) -> None:
        idx = combo.get_selected()
        if 0 <= idx < len(self.devices):
            new_dev = self.devices[idx]
            if self.current_device and self.current_device.device_id == new_dev.device_id:
                return
            self.current_device = new_dev
            self.config.selected_device_id = new_dev.device_id
            self.config.save()
            self.reload_lineup(auto_refresh_epg=True)

    def reload_lineup(self, auto_refresh_epg: bool = False) -> None:
        dev = self.current_device
        if not dev:
            self._propagate_state_to_views()
            return

        def _worker() -> None:
            lineup = self.client.get_lineup(dev, show_all=True)
            need_epg = auto_refresh_epg and (
                not self.epg.channels or (time.time() - self.epg.last_updated > 7200)
            )
            if need_epg:
                self.epg.refresh_epg(dev)
            GLib.idle_add(self._on_lineup_loaded, lineup)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_lineup_loaded(self, lineup: List[ChannelItem]) -> None:
        self.channels = lineup
        self._propagate_state_to_views()

    def _propagate_state_to_views(self) -> None:
        self.live_view.set_device_and_channels(self.current_device, self.channels)
        self.guide_view.set_device_and_channels(self.current_device, self.channels)
        self.channels_view.set_device_and_channels(self.current_device, self.channels)
        self.signal_view.set_device_and_channels(self.current_device, self.channels)
        self.dvr_view.set_device_and_channels(self.current_device, self.channels)
        self.settings_view.set_device(self.current_device)

    def toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode across the application."""
        if self.is_fullscreen():
            self.unfullscreen()
            self.btn_window_fullscreen.set_icon_name("view-fullscreen-symbolic")
            self.btn_window_fullscreen.set_tooltip_text("Enter Fullscreen (F11)")
        else:
            self.fullscreen()
            self.btn_window_fullscreen.set_icon_name("view-restore-symbolic")
            self.btn_window_fullscreen.set_tooltip_text("Exit Fullscreen (F11 / Esc)")

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        # F11 toggles fullscreen
        if keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        # Escape exits fullscreen if active
        if keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.toggle_fullscreen()
            return True
        # Ctrl+B toggles channel sidebar in Live TV
        if (state & Gdk.ModifierType.CONTROL_MASK) and keyval in (Gdk.KEY_b, Gdk.KEY_B):
            if self.view_stack.get_visible_child_name() == "live":
                self.live_view.toggle_sidebar()
                return True
        return False

    def _on_close_request(self, *_args: object) -> bool:
        self.live_view.stop_playback()
        if self.signal_view.probe_session:
            self.signal_view.probe_session.stop()
        return False

