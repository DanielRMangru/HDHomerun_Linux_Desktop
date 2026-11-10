"""Real-Time RF Signal Meter & Hardware Tuner Diagnostics View."""

from __future__ import annotations

import cairo
import math
import threading
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk

from ..backend.client import (
    ChannelItem,
    HDHomeRunClient,
    HDHomeRunDevice,
    SignalProbeSession,
    TunerTelemetry,
)
from ..config import AppConfig


class SignalView(Gtk.Box):
    """Interactive RF signal meter with Cairo gauges, rolling history chart, and tuner cards."""

    def __init__(
        self,
        config: AppConfig,
        client: HDHomeRunClient,
        on_toast: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(14)
        self.set_margin_end(14)

        self.config = config
        self.client = client
        self.on_toast = on_toast

        self.device: Optional[HDHomeRunDevice] = None
        self.channels: List[ChannelItem] = []
        self.tuners: List[TunerTelemetry] = []
        self.probe_session: Optional[SignalProbeSession] = None

        # Rolling 60-point history of (ss, snq, seq) for the active/focused tuner
        self.history: Deque[Tuple[int, int, int]] = deque(maxlen=60)
        self.focused_tuner_index: int = 0

        self._build_ui()
        GLib.timeout_add(1000, self._poll_tuners)

    def _build_ui(self) -> None:
        # --- Top Probe Control Bar ---
        ctrl_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ctrl_bar.add_css_class("card-panel")

        lbl = Gtk.Label(label="Channel to Test:")
        lbl.add_css_class("heading")
        ctrl_bar.append(lbl)

        self.channel_combo = Gtk.DropDown.new_from_strings(["Select channel..."])
        self.channel_combo.set_size_request(240, -1)
        ctrl_bar.append(self.channel_combo)

        lbl_rf = Gtk.Label(label="or RF/Ch:")
        ctrl_bar.append(lbl_rf)

        self.entry_manual_rf = Gtk.Entry()
        self.entry_manual_rf.set_placeholder_text("e.g. 3.1 or auto:24")
        self.entry_manual_rf.set_size_request(140, -1)
        ctrl_bar.append(self.entry_manual_rf)

        self.btn_probe = Gtk.Button(label="Start Live Signal Probe")
        self.btn_probe.set_icon_name("network-cellular-signal-excellent-symbolic")
        self.btn_probe.add_css_class("suggested-action")
        self.btn_probe.connect("clicked", self._on_toggle_probe)
        ctrl_bar.append(self.btn_probe)

        self.btn_stop_probe = Gtk.Button(label="Release Probe")
        self.btn_stop_probe.set_icon_name("media-playback-stop-symbolic")
        self.btn_stop_probe.add_css_class("destructive-action")
        self.btn_stop_probe.set_sensitive(False)
        self.btn_stop_probe.connect("clicked", self._on_stop_probe)
        ctrl_bar.append(self.btn_stop_probe)

        self.btn_sweep = Gtk.Button(label="Sweep All Channels")
        self.btn_sweep.set_icon_name("view-refresh-symbolic")
        self.btn_sweep.set_tooltip_text("Test live signal across all active channels sequentially")
        self.btn_sweep.connect("clicked", self._on_sweep_all_clicked)
        ctrl_bar.append(self.btn_sweep)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        ctrl_bar.append(spacer)

        self.lbl_probe_status = Gtk.Label(label="Passive Monitoring")
        self.lbl_probe_status.add_css_class("mono-value")
        ctrl_bar.append(self.lbl_probe_status)

        self.append(ctrl_bar)

        # --- Middle: Cairo Real-Time Signal Gauges & Rolling Chart ---
        chart_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        chart_card.add_css_class("card-panel")

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.lbl_focused_tuner = Gtk.Label(
            label="Real-Time RF Signal Telemetry (Tuner 0)"
        )
        self.lbl_focused_tuner.add_css_class("heading")
        self.lbl_focused_tuner.set_halign(Gtk.Align.START)
        header_row.append(self.lbl_focused_tuner)

        h_spacer = Gtk.Box(hexpand=True)
        header_row.append(h_spacer)

        self.lbl_rf_details = Gtk.Label(label="Freq: —  |  Lock: none  |  Bitrate: 0.00 Mbps")
        self.lbl_rf_details.add_css_class("mono-value")
        header_row.append(self.lbl_rf_details)
        chart_card.append(header_row)

        self.chart_picture = Gtk.Picture()
        self.chart_picture.set_size_request(-1, 225)
        self.chart_picture.set_hexpand(True)
        self.chart_picture.set_can_shrink(True)
        chart_card.append(self.chart_picture)

        self.append(chart_card)

        # --- Bottom: Per-Tuner Hardware Cards & Lineup Signal Table ---
        bottom_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        bottom_paned.set_position(480)
        bottom_paned.set_vexpand(True)

        # Left of bottom: Per-Tuner Status Cards
        tuner_scroll = Gtk.ScrolledWindow()
        tuner_scroll.set_vexpand(True)
        self.tuner_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.tuner_cards_box.set_margin_end(8)
        tuner_scroll.set_child(self.tuner_cards_box)
        bottom_paned.set_start_child(tuner_scroll)

        # Right of bottom: All Channels Signal Strength & Quality Table
        table_scroll = Gtk.ScrolledWindow()
        table_scroll.set_vexpand(True)
        self.channel_sig_list = Gtk.ListBox()
        self.channel_sig_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.channel_sig_list.add_css_class("boxed-list")
        table_scroll.set_child(self.channel_sig_list)
        bottom_paned.set_end_child(table_scroll)

        self.append(bottom_paned)

    def set_device_and_channels(
        self, device: Optional[HDHomeRunDevice], channels: List[ChannelItem]
    ) -> None:
        self.device = device
        self.channels = channels
        if self.probe_session:
            self.probe_session.stop()
        self.probe_session = SignalProbeSession(device) if device else None

        active_items = [
            f"{ch.guide_number} — {ch.guide_name}"
            for ch in channels
            if not ch.blocked and ch.subscribed
        ]
        if not active_items:
            active_items = [f"{ch.guide_number} — {ch.guide_name}" for ch in channels]
        if not active_items:
            active_items = ["No channels available"]

        self.channel_combo.set_model(Gtk.StringList.new(active_items))
        self._rebuild_channel_signal_list()

    def probe_specific_channel(self, guide_number: str) -> None:
        """Called externally (e.g. from Channels or Live TV view) to immediately probe a channel."""
        if not self.device:
            return
        if guide_number:
            self.entry_manual_rf.set_text(guide_number)
            for idx, ch in enumerate(
                [c for c in self.channels if not c.blocked and c.subscribed]
            ):
                if ch.guide_number == guide_number:
                    self.channel_combo.set_selected(idx)
                    break
        self._start_probe(guide_number)

    def _on_toggle_probe(self, _btn: Gtk.Button) -> None:
        manual = self.entry_manual_rf.get_text().strip()
        if manual:
            target = manual
        else:
            sel = self.channel_combo.get_selected_item()
            if not sel:
                return
            text = sel.get_string()
            target = text.split("—")[0].strip()
        self._start_probe(target)

    def _start_probe(self, target: str) -> None:
        if not self.device or not self.probe_session:
            self.on_toast("No HDHomeRun device connected.")
            return

        self.history.clear()
        if ":" in target or target.isdigit():
            # If user entered raw RF channel (e.g. auto:24) vs virtual channel (e.g. 3.1)
            if "." not in target and (":" in target or int(target) > 1):
                err = self.probe_session.start_rf_channel_probe(0, target)
                if err:
                    self.on_toast(f"RF tune error: {err}")
                    return
            else:
                self.probe_session.start_vchannel_probe(target)
        else:
            self.probe_session.start_vchannel_probe(target)

        self.btn_stop_probe.set_sensitive(True)
        self.lbl_probe_status.set_text(f"Probing {target}...")
        self.on_toast(f"Started live RF signal probe on {target}")

    def _on_stop_probe(self, *_args: object) -> None:
        self._sweep_cancelled = True
        if self.probe_session:
            self.probe_session.stop()
        self.btn_stop_probe.set_sensitive(False)
        self.lbl_probe_status.set_text("Passive Monitoring")
        self.on_toast("Stopped signal probe and released tuner.")

    def _on_sweep_all_clicked(self, _btn: Gtk.Button) -> None:
        if not self.device:
            self.on_toast("No HDHomeRun device connected.")
            return
        active_chs = [c for c in self.channels if not c.blocked and c.subscribed]
        if not active_chs:
            self.on_toast("No active channels to sweep.")
            return
        self._sweep_cancelled = False
        self.btn_sweep.set_sensitive(False)
        self.btn_stop_probe.set_sensitive(True)
        self.on_toast(f"Sweeping live RF signal across {len(active_chs)} active channels...")

        def _sweep_worker() -> None:
            for idx, ch in enumerate(active_chs, 1):
                if getattr(self, "_sweep_cancelled", False) or not self.device:
                    break
                GLib.idle_add(
                    self.lbl_probe_status.set_text,
                    f"Sweep [{idx}/{len(active_chs)}]: {ch.guide_number} {ch.guide_name}",
                )
                tele = self.client.quick_probe_channel_signal(
                    self.device, ch.guide_number, dwell_seconds=1.2
                )
                if tele and tele.signal_strength > 0:
                    ch.signal_strength = tele.signal_strength
                    ch.signal_quality = tele.signal_quality
                    GLib.idle_add(self._rebuild_channel_signal_list)
            GLib.idle_add(self._finish_sweep)

        threading.Thread(target=_sweep_worker, daemon=True).start()

    def _finish_sweep(self) -> None:
        self.btn_sweep.set_sensitive(True)
        self.btn_stop_probe.set_sensitive(False)
        self.lbl_probe_status.set_text("Sweep Complete")
        self.on_toast("Channel signal sweep complete.")

    def _poll_tuners(self) -> bool:
        if not self.device:
            return True

        def _worker() -> None:
            if not self.device:
                return
            tuners = self.client.get_tuners_telemetry(self.device)
            GLib.idle_add(self._update_telemetry_ui, tuners)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def _update_telemetry_ui(self, tuners: List[TunerTelemetry]) -> None:
        self.tuners = tuners

        # Pick active tuner if one is in use
        chosen: Optional[TunerTelemetry] = None
        for t in tuners:
            if t.in_use:
                chosen = t
                self.focused_tuner_index = t.tuner_index
                break
        if chosen is None and tuners:
            chosen = tuners[0]

        if chosen:
            self.history.append(
                (chosen.signal_strength, chosen.signal_quality, chosen.symbol_quality)
            )
            ch_desc = (
                f" — Ch {chosen.vct_number} ({chosen.vct_name})"
                if chosen.vct_number
                else ""
            )
            self.lbl_focused_tuner.set_text(
                f"Real-Time RF Signal Telemetry ({chosen.resource}{ch_desc})"
            )
            self.lbl_rf_details.set_text(
                f"Freq: {chosen.frequency_mhz_str}  |  Lock: {chosen.lock}  |  "
                f"Bitrate: {chosen.bitrate_mbps_str} ({chosen.packets_per_sec} pps)"
            )

        self._render_signal_dashboard()
        self._rebuild_tuner_cards()

    def _rebuild_tuner_cards(self) -> None:
        while True:
            child = self.tuner_cards_box.get_first_child()
            if not child:
                break
            self.tuner_cards_box.remove(child)

        for t in self.tuners:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("tuner-card")

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(
                label=f"{t.resource.upper()} — {'ACTIVE' if t.in_use else 'IDLE'}"
            )
            title.add_css_class("heading")
            top.append(title)

            if t.vct_number:
                vch_badge = Gtk.Label(label=f"{t.vct_number} {t.vct_name}")
                vch_badge.add_css_class("channel-number-badge")
                top.append(vch_badge)

            spacer = Gtk.Box(hexpand=True)
            top.append(spacer)

            freq_lbl = Gtk.Label(label=t.frequency_mhz_str)
            freq_lbl.add_css_class("mono-value")
            top.append(freq_lbl)
            card.append(top)

            metrics_lbl = Gtk.Label(
                label=(
                    f"Strength (SS): {t.signal_strength}%   •   "
                    f"SNR Quality (SNQ): {t.signal_quality}%   •   "
                    f"Symbol Quality (SEQ): {t.symbol_quality}%"
                )
            )
            metrics_lbl.set_halign(Gtk.Align.START)
            metrics_lbl.add_css_class("caption")
            card.append(metrics_lbl)

            net_lbl = Gtk.Label(
                label=(
                    f"Lock: {t.lock}   •   Stream Rate: {t.bitrate_mbps_str} ({t.packets_per_sec} pps)   •   "
                    f"Target IP: {t.target_ip or 'none'}"
                )
            )
            net_lbl.set_halign(Gtk.Align.START)
            net_lbl.add_css_class("caption")
            net_lbl.add_css_class("dim-label")
            card.append(net_lbl)

            if t.programs:
                mux_str = "Mux Subchannels: " + ", ".join(t.programs[:6])
                mux_lbl = Gtk.Label(label=mux_str)
                mux_lbl.set_halign(Gtk.Align.START)
                mux_lbl.add_css_class("caption")
                card.append(mux_lbl)

            self.tuner_cards_box.append(card)

    def _rebuild_channel_signal_list(self) -> None:
        while True:
            child = self.channel_sig_list.get_first_child()
            if not child:
                break
            self.channel_sig_list.remove(child)

        for ch in self.channels:
            if not ch.subscribed and not self.config.show_unsubscribed_channels:
                continue
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)
            hbox.set_margin_start(10)
            hbox.set_margin_end(10)

            num = Gtk.Label(label=ch.guide_number)
            num.set_size_request(54, -1)
            num.add_css_class("channel-number-badge")
            hbox.append(num)

            name = Gtk.Label(label=ch.guide_name, halign=Gtk.Align.START, hexpand=True)
            hbox.append(name)

            sig = Gtk.Label(label=f"SS {ch.signal_strength}% / SNQ {ch.signal_quality}%")
            sig.add_css_class("mono-value")
            if ch.signal_quality >= 60:
                sig.add_css_class("signal-good")
            elif ch.signal_quality >= 35:
                sig.add_css_class("signal-warn")
            else:
                sig.add_css_class("signal-bad")
            hbox.append(sig)

            btn_test = Gtk.Button(label="Probe")
            btn_test.connect("clicked", lambda _b, g=ch.guide_number: self.probe_specific_channel(g))
            hbox.append(btn_test)

            row.set_child(hbox)
            self.channel_sig_list.append(row)

    def _render_signal_dashboard(self) -> None:
        """Render 3 radial/arc gauges on the left and a 60s rolling line chart on the right using Cairo ImageSurface."""
        width = max(800, self.get_width() or 800)
        height = 225

        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)

        ss, snq, seq = self.history[-1] if self.history else (0, 0, 0)

        # Background
        cr.set_source_rgba(0.08, 0.09, 0.12, 0.95)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Draw 3 circular arc gauges on the left ~420px
        gauges = [
            ("Signal Strength (SS)", ss, (0.20, 0.65, 1.0)),
            ("SNR Quality (SNQ)", snq, (0.18, 0.80, 0.49)),
            ("Symbol Quality (SEQ)", seq, (0.96, 0.76, 0.07)),
        ]
        gauge_area_w = min(430, int(width * 0.48))
        spacing = gauge_area_w / 3.0
        radius = min(54.0, (spacing * 0.42), (height * 0.34))
        cy = height * 0.52

        for idx, (label, val, rgb) in enumerate(gauges):
            cx = (idx + 0.5) * spacing
            start_ang = 0.75 * math.pi
            end_ang = 2.25 * math.pi

            # Track arc
            cr.set_line_width(10.0)
            cr.set_source_rgba(0.25, 0.27, 0.32, 0.7)
            cr.arc(cx, cy, radius, start_ang, end_ang)
            cr.stroke()

            # Value arc
            frac = max(0.0, min(1.0, val / 100.0))
            val_ang = start_ang + frac * (end_ang - start_ang)
            if val >= 70:
                cr.set_source_rgb(*rgb)
            elif val >= 40:
                cr.set_source_rgb(0.96, 0.76, 0.07)
            else:
                cr.set_source_rgb(0.88, 0.11, 0.14)
            cr.arc(cx, cy, radius, start_ang, val_ang)
            cr.stroke()

            # Percentage text
            cr.set_source_rgb(0.95, 0.96, 0.98)
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(20.0)
            pct_str = f"{val}%"
            ext = cr.text_extents(pct_str)
            cr.move_to(cx - ext.width / 2.0, cy + ext.height / 3.0)
            cr.show_text(pct_str)

            # Label below
            cr.set_source_rgba(0.75, 0.78, 0.84, 0.95)
            cr.set_font_size(11.0)
            lext = cr.text_extents(label)
            cr.move_to(cx - lext.width / 2.0, cy + radius + 22.0)
            cr.show_text(label)

        # Right side: Rolling 60-second history graph
        chart_x = gauge_area_w + 20
        chart_w = max(100, width - chart_x - 18)
        chart_y = 22
        chart_h = height - 48

        # Grid lines (0%, 25%, 50%, 75%, 100%)
        cr.set_line_width(1.0)
        for pct in (0, 25, 50, 75, 100):
            gy = chart_y + chart_h * (1.0 - pct / 100.0)
            cr.set_source_rgba(0.3, 0.33, 0.4, 0.35)
            cr.move_to(chart_x, gy)
            cr.line_to(chart_x + chart_w, gy)
            cr.stroke()

            cr.set_source_rgba(0.65, 0.68, 0.74, 0.8)
            cr.set_font_size(9.5)
            cr.move_to(chart_x - 26, gy + 3)
            cr.show_text(f"{pct}%")

        # Plot history lines for SS (blue), SNQ (green), SEQ (gold)
        pts = list(self.history)
        if len(pts) >= 2:
            series_cfg = [
                (0, (0.25, 0.68, 1.0), "SS"),
                (1, (0.18, 0.85, 0.50), "SNQ"),
                (2, (0.98, 0.78, 0.12), "SEQ"),
            ]
            for s_idx, color, _name in series_cfg:
                cr.set_source_rgb(*color)
                cr.set_line_width(2.2)
                for i, tup in enumerate(pts):
                    px = chart_x + (i / 59.0) * chart_w
                    py = chart_y + chart_h * (1.0 - max(0, min(100, tup[s_idx])) / 100.0)
                    if i == 0:
                        cr.move_to(px, py)
                    else:
                        cr.line_to(px, py)
                cr.stroke()

        surface.flush()
        data = surface.get_data()
        gbytes = GLib.Bytes.new(data.tobytes())
        texture = Gdk.MemoryTexture.new(
            width, height, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, gbytes, surface.get_stride()
        )
        self.chart_picture.set_paintable(texture)

