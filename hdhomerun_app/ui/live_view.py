"""Live TV Playback View with embedded GStreamer GTK4 player, VLC launcher, EPG bar, and instant DVR."""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, GLib, Gst, Gtk, Pango

from ..backend.client import ChannelItem, HDHomeRunClient, HDHomeRunDevice, TunerTelemetry
from ..backend.epg import EPGManager
from ..backend.recorder import DVRManager
from ..config import AppConfig


Gst.init(None)


class LiveTVView(Gtk.Box):
    """Split pane view: left channel selector with On-Now EPG, right embedded player & controls."""

    def __init__(
        self,
        config: AppConfig,
        client: HDHomeRunClient,
        epg: EPGManager,
        dvr: DVRManager,
        on_toast: Callable[[str], None],
        on_inspect_signal: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.config = config
        self.client = client
        self.epg = epg
        self.dvr = dvr
        self.on_toast = on_toast
        self.on_inspect_signal = on_inspect_signal

        self.device: Optional[HDHomeRunDevice] = None
        self.channels: List[ChannelItem] = []
        self.current_channel: Optional[ChannelItem] = None
        self._filter_mode: str = "all"  # all | fav | hd

        # GStreamer pipeline state
        self._playbin: Optional[Gst.Element] = None
        self._sink: Optional[Gst.Element] = None
        self._is_playing: bool = False

        self._build_ui()
        self._init_gstreamer()

        # Poll active tuner signal every 2s while playing
        GLib.timeout_add_seconds(2, self._poll_live_telemetry)

    def _build_ui(self) -> None:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(340)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        self.append(paned)

        # --- Left Sidebar: Channel Switcher ---
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_margin_top(10)
        left_box.set_margin_bottom(10)
        left_box.set_margin_start(10)
        left_box.set_margin_end(6)
        left_box.set_size_request(290, -1)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Filter channels or shows...")
        self.search_entry.connect("search-changed", lambda *_: self._populate_channel_list())
        left_box.append(self.search_entry)

        # Filter pills
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_filter_all = Gtk.ToggleButton(label="All Active")
        self.btn_filter_all.set_active(True)
        self.btn_filter_fav = Gtk.ToggleButton(label="Favorites")
        self.btn_filter_fav.set_group(self.btn_filter_all)
        self.btn_filter_hd = Gtk.ToggleButton(label="HD Only")
        self.btn_filter_hd.set_group(self.btn_filter_all)

        for btn, mode in [
            (self.btn_filter_all, "all"),
            (self.btn_filter_fav, "fav"),
            (self.btn_filter_hd, "hd"),
        ]:
            btn.set_hexpand(True)
            btn.connect("toggled", self._on_filter_toggled, mode)
            filter_box.append(btn)
        left_box.append(filter_box)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self.listbox)
        left_box.append(scrolled)

        paned.set_start_child(left_box)

        # --- Right Area: Video Player + Controls + EPG + Signal ---
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right_box.set_margin_top(10)
        right_box.set_margin_bottom(10)
        right_box.set_margin_start(6)
        right_box.set_margin_end(12)
        right_box.set_hexpand(True)
        right_box.set_vexpand(True)

        # Video container (Overlay with placeholder when idle)
        self.video_overlay = Gtk.Overlay()
        self.video_overlay.set_hexpand(True)
        self.video_overlay.set_vexpand(True)
        self.video_overlay.add_css_class("video-surface")

        self.video_picture = Gtk.Picture()
        self.video_picture.set_hexpand(True)
        self.video_picture.set_vexpand(True)
        self.video_picture.set_can_shrink(True)
        self.video_overlay.set_child(self.video_picture)

        self.placeholder_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        icon.set_pixel_size(64)
        icon.add_css_class("dim-label")
        self.placeholder_box.append(icon)

        self.placeholder_label = Gtk.Label(
            label="Select a channel on the left to watch Live TV\nor launch in VLC Media Player"
        )
        self.placeholder_label.set_justify(Gtk.Justification.CENTER)
        self.placeholder_label.add_css_class("title-4")
        self.placeholder_label.add_css_class("dim-label")
        self.placeholder_box.append(self.placeholder_label)
        self.video_overlay.add_overlay(self.placeholder_box)

        right_box.append(self.video_overlay)

        # Transport & Action Controls Bar
        ctrl_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctrl_bar.add_css_class("card-panel")

        self.btn_play_stop = Gtk.Button()
        self.btn_play_stop.set_icon_name("media-playback-start-symbolic")
        self.btn_play_stop.set_tooltip_text("Play / Stop in Embedded Player")
        self.btn_play_stop.add_css_class("suggested-action")
        self.btn_play_stop.connect("clicked", self._on_toggle_play)
        ctrl_bar.append(self.btn_play_stop)

        self.btn_mute = Gtk.ToggleButton()
        self.btn_mute.set_icon_name("audio-volume-high-symbolic")
        self.btn_mute.set_tooltip_text("Mute / Unmute Audio")
        self.btn_mute.connect("toggled", self._on_mute_toggled)
        ctrl_bar.append(self.btn_mute)

        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 5.0)
        self.vol_scale.set_value(85.0)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.set_size_request(110, -1)
        self.vol_scale.connect("value-changed", self._on_volume_changed)
        ctrl_bar.append(self.vol_scale)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        ctrl_bar.append(spacer)

        # Instant DVR Record button
        self.btn_instant_rec = Gtk.Button(label="Record Channel")
        self.btn_instant_rec.set_icon_name("media-record-symbolic")
        self.btn_instant_rec.add_css_class("destructive-action")
        self.btn_instant_rec.connect("clicked", self._on_instant_record_clicked)
        ctrl_bar.append(self.btn_instant_rec)

        # Check Signal button
        self.btn_signal = Gtk.Button(label="Signal Meter")
        self.btn_signal.set_icon_name("network-cellular-signal-excellent-symbolic")
        self.btn_signal.connect("clicked", self._on_open_signal_clicked)
        ctrl_bar.append(self.btn_signal)

        # Open in External Player (VLC / mpv)
        self.btn_external = Gtk.Button(label="Open in VLC")
        self.btn_external.set_icon_name("external-link-symbolic")
        self.btn_external.connect("clicked", self._on_open_external_player)
        ctrl_bar.append(self.btn_external)

        right_box.append(ctrl_bar)

        # Info & Live Signal Card
        info_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_card.add_css_class("card-panel")

        top_info_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.lbl_now_channel = Gtk.Label(label="No Channel Selected")
        self.lbl_now_channel.add_css_class("title-3")
        self.lbl_now_channel.set_halign(Gtk.Align.START)
        top_info_row.append(self.lbl_now_channel)

        info_spacer = Gtk.Box()
        info_spacer.set_hexpand(True)
        top_info_row.append(info_spacer)

        self.lbl_live_signal = Gtk.Label(label="Signal: Idle")
        self.lbl_live_signal.add_css_class("mono-value")
        top_info_row.append(self.lbl_live_signal)
        info_card.append(top_info_row)

        self.lbl_now_program = Gtk.Label(label="Choose a channel to view program details and live stream.")
        self.lbl_now_program.set_halign(Gtk.Align.START)
        self.lbl_now_program.set_wrap(True)
        self.lbl_now_program.set_ellipsize(Pango.EllipsizeMode.END)
        info_card.append(self.lbl_now_program)

        self.lbl_now_synopsis = Gtk.Label(label="")
        self.lbl_now_synopsis.set_halign(Gtk.Align.START)
        self.lbl_now_synopsis.set_wrap(True)
        self.lbl_now_synopsis.add_css_class("dim-label")
        info_card.append(self.lbl_now_synopsis)

        right_box.append(info_card)
        paned.set_end_child(right_box)

    def _init_gstreamer(self) -> None:
        try:
            self._playbin = Gst.ElementFactory.make("playbin", "player")
            self._sink = Gst.ElementFactory.make("gtk4paintablesink", "gtksink")
            if self._playbin and self._sink:
                paintable = self._sink.get_property("paintable")
                self.video_picture.set_paintable(paintable)
                glsink = Gst.ElementFactory.make("glsinkbin", "glsinkbin")
                if glsink:
                    glsink.set_property("sink", self._sink)
                    self._playbin.set_property("video-sink", glsink)
                else:
                    self._playbin.set_property("video-sink", self._sink)
                self._playbin.set_property("volume", 0.85)

                bus = self._playbin.get_bus()
                bus.add_signal_watch()
                bus.connect("message::error", self._on_gst_error)
        except Exception:
            self._playbin = None

    def _on_gst_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, _debug = msg.parse_error()
        self.stop_playback()
        self.on_toast(f"Stream playback error: {err.message}. Try 'Open in VLC'.")

    def set_device_and_channels(self, device: Optional[HDHomeRunDevice], channels: List[ChannelItem]) -> None:
        self.device = device
        self.channels = channels
        self._populate_channel_list()

    def _on_filter_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if btn.get_active():
            self._filter_mode = mode
            self._populate_channel_list()

    def _populate_channel_list(self) -> None:
        while True:
            row = self.listbox.get_first_child()
            if not row:
                break
            self.listbox.remove(row)

        query = self.search_entry.get_text().strip().lower()
        for ch in self.channels:
            if ch.blocked and not self.config.show_blocked_channels:
                continue
            if not ch.subscribed and not self.config.show_unsubscribed_channels:
                continue
            if self._filter_mode == "fav" and not ch.favorite:
                continue
            if self._filter_mode == "hd" and not ch.hd:
                continue

            now_prog, _ = self.epg.get_now_and_next(ch.guide_number)
            prog_title = now_prog.title if now_prog else ""

            if query:
                haystack = f"{ch.guide_number} {ch.guide_name} {prog_title}".lower()
                if query not in haystack:
                    continue

            row = Gtk.ListBoxRow()
            row._channel_item = ch  # type: ignore[attr-defined]

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.set_margin_top(7)
            vbox.set_margin_bottom(7)
            vbox.set_margin_start(10)
            vbox.set_margin_end(10)

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            badge = Gtk.Label(label=ch.guide_number)
            badge.add_css_class("channel-number-badge")
            top.append(badge)

            name_lbl = Gtk.Label(label=ch.guide_name)
            name_lbl.set_halign(Gtk.Align.START)
            name_lbl.set_hexpand(True)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            name_lbl.add_css_class("heading")
            top.append(name_lbl)

            if self.dvr.is_channel_recording(ch.guide_number):
                rec_tag = Gtk.Label(label="REC")
                rec_tag.add_css_class("tag-rec")
                top.append(rec_tag)

            if ch.favorite:
                fav_tag = Gtk.Label(label="★")
                fav_tag.add_css_class("tag-fav")
                top.append(fav_tag)

            if ch.hd:
                hd_tag = Gtk.Label(label="HD")
                hd_tag.add_css_class("tag-hd")
                top.append(hd_tag)

            vbox.append(top)

            if now_prog:
                sub = Gtk.Label(label=f"Now: {now_prog.title} ({now_prog.time_range_str})")
                sub.set_halign(Gtk.Align.START)
                sub.set_ellipsize(Pango.EllipsizeMode.END)
                sub.add_css_class("caption")
                sub.add_css_class("dim-label")
                vbox.append(sub)

            row.set_child(vbox)
            self.listbox.append(row)

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        ch: Optional[ChannelItem] = getattr(row, "_channel_item", None)
        if ch:
            self.play_channel(ch)

    def play_channel(self, channel: ChannelItem) -> None:
        self.current_channel = channel
        self._update_channel_info_panel()

        if not self._playbin:
            self.on_toast("Embedded GStreamer sink unavailable; opening in external player.")
            self._on_open_external_player()
            return

        self._playbin.set_state(Gst.State.NULL)
        self._playbin.set_property("uri", channel.url)
        ret = self._playbin.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._is_playing = False
            self.placeholder_box.set_visible(True)
            self.on_toast(f"Could not start embedded playback for {channel.guide_number}; try 'Open in VLC'.")
            return

        self._is_playing = True
        self.placeholder_box.set_visible(False)
        self.btn_play_stop.set_icon_name("media-playback-stop-symbolic")

    def stop_playback(self) -> None:
        if self._playbin:
            self._playbin.set_state(Gst.State.NULL)
        self._is_playing = False
        self.placeholder_box.set_visible(True)
        self.btn_play_stop.set_icon_name("media-playback-start-symbolic")

    def _on_toggle_play(self, _btn: Gtk.Button) -> None:
        if self._is_playing:
            self.stop_playback()
        elif self.current_channel:
            self.play_channel(self.current_channel)
        else:
            first_row = self.listbox.get_row_at_index(0)
            if first_row:
                self.listbox.select_row(first_row)
                ch = getattr(first_row, "_channel_item", None)
                if ch:
                    self.play_channel(ch)

    def _on_mute_toggled(self, btn: Gtk.ToggleButton) -> None:
        muted = btn.get_active()
        btn.set_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        )
        if self._playbin:
            self._playbin.set_property("mute", muted)

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        val = scale.get_value() / 100.0
        if self._playbin:
            self._playbin.set_property("volume", val)

    def _on_open_external_player(self, *_args: object) -> None:
        if not self.current_channel:
            self.on_toast("Select a channel first.")
            return
        url = self.current_channel.url
        preferred = self.config.external_player or "vlc"
        candidates = [preferred, "vlc", "mpv", "ffplay"]
        for cmd_name in candidates:
            bin_path = shutil.which(cmd_name)
            if bin_path:
                # Stop embedded playback so we don't consume two tuners unnecessarily
                self.stop_playback()
                try:
                    subprocess.Popen(
                        [bin_path, url],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.on_toast(
                        f"Opened {self.current_channel.guide_number} ({self.current_channel.guide_name}) in {cmd_name}."
                    )
                    return
                except Exception as exc:
                    self.on_toast(f"Failed to launch {cmd_name}: {exc}")
                    return
        self.on_toast("No external player (vlc, mpv, ffplay) found on PATH.")

    def _on_instant_record_clicked(self, _btn: Gtk.Button) -> None:
        if not self.current_channel:
            self.on_toast("Select a channel first to start recording.")
            return
        ch = self.current_channel
        active_rec = self.dvr.is_channel_recording(ch.guide_number)
        if active_rec:
            self.dvr.stop_or_cancel_recording(active_rec.id)
            self.on_toast(f"Stopped recording on channel {ch.guide_number}.")
            self._update_channel_info_panel()
            self._populate_channel_list()
            return

        now_prog, _ = self.epg.get_now_and_next(ch.guide_number)
        if now_prog:
            rec = self.dvr.schedule_recording(
                title=now_prog.title,
                channel_number=ch.guide_number,
                channel_name=ch.guide_name,
                stream_url=ch.url,
                start_time=now_prog.start_time,
                end_time=now_prog.end_time,
                episode_title=now_prog.episode_title,
                episode_number=now_prog.episode_number,
                synopsis=now_prog.synopsis,
            )
        else:
            rec = self.dvr.start_instant_recording(
                channel_number=ch.guide_number,
                channel_name=ch.guide_name,
                stream_url=ch.url,
                duration_minutes=60,
            )
        self.on_toast(f"Recording started: {rec.title} → {rec.output_path}")
        self._update_channel_info_panel()
        self._populate_channel_list()

    def _on_open_signal_clicked(self, _btn: Gtk.Button) -> None:
        ch_num = self.current_channel.guide_number if self.current_channel else ""
        self.on_inspect_signal(ch_num)

    def _update_channel_info_panel(self) -> None:
        ch = self.current_channel
        if not ch:
            return
        self.lbl_now_channel.set_text(
            f"Channel {ch.guide_number} — {ch.guide_name}"
        )
        active_rec = self.dvr.is_channel_recording(ch.guide_number)
        if active_rec:
            self.btn_instant_rec.set_label("Stop Recording")
        else:
            self.btn_instant_rec.set_label("Record Channel")

        now_prog, next_prog = self.epg.get_now_and_next(ch.guide_number)
        if now_prog:
            ep_info = f" ({now_prog.episode_number})" if now_prog.episode_number else ""
            next_str = f"   |   Next: {next_prog.title} ({next_prog.time_range_str})" if next_prog else ""
            self.lbl_now_program.set_text(
                f"On Now: {now_prog.title}{ep_info} [{now_prog.time_range_str}]{next_str}"
            )
            self.lbl_now_synopsis.set_text(now_prog.synopsis or "")
        else:
            self.lbl_now_program.set_text(
                f"Stream URL: {ch.url}  •  Video: {ch.video_codec or 'Auto'}  •  Audio: {ch.audio_codec or 'Auto'}"
            )
            self.lbl_now_synopsis.set_text(
                f"Scan Signal Strength: {ch.signal_strength}%  |  Signal Quality: {ch.signal_quality}%"
            )

    def _poll_live_telemetry(self) -> bool:
        if not self.device or not self.current_channel:
            return True
        if not self._is_playing:
            self.lbl_live_signal.set_text(
                f"Scan SS: {self.current_channel.signal_strength}% | SNQ: {self.current_channel.signal_quality}%"
            )
            return True

        def _fetch() -> None:
            if not self.device or not self.current_channel:
                return
            tuners = self.client.get_tuners_telemetry(self.device)
            matched: Optional[TunerTelemetry] = None
            for t in tuners:
                if t.vct_number == self.current_channel.guide_number or t.in_use:
                    matched = t
                    if t.vct_number == self.current_channel.guide_number:
                        break
            if matched:
                text = (
                    f"{matched.resource}: SS {matched.signal_strength}% | "
                    f"SNQ {matched.signal_quality}% | SEQ {matched.symbol_quality}% | "
                    f"{matched.bitrate_mbps_str}"
                )
                GLib.idle_add(self.lbl_live_signal.set_text, text)

        import threading
        threading.Thread(target=_fetch, daemon=True).start()
        return True

