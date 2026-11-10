"""Interactive Electronic Program Guide (EPG) Grid, Search, and DVR Scheduling View."""

from __future__ import annotations

import datetime
import threading
import time
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Pango

from ..backend.client import ChannelItem, HDHomeRunDevice
from ..backend.epg import EPGManager, ProgramEntry
from ..backend.recorder import DVRManager
from ..config import AppConfig


PIXELS_PER_MINUTE = 4.5
WINDOW_MINUTES = 180  # 3-hour visible window per page


class GuideView(Gtk.Box):
    """EPG timeline grid with time navigation, program search, and one-click DVR scheduling."""

    def __init__(
        self,
        config: AppConfig,
        epg: EPGManager,
        dvr: DVRManager,
        on_watch_channel: Callable[[ChannelItem], None],
        on_toast: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(14)
        self.set_margin_end(14)

        self.config = config
        self.epg = epg
        self.dvr = dvr
        self.on_watch_channel = on_watch_channel
        self.on_toast = on_toast

        self.device: Optional[HDHomeRunDevice] = None
        self.channels: List[ChannelItem] = []
        self.selected_program: Optional[ProgramEntry] = None
        self.window_start_ts: int = self._snap_to_half_hour(time.time())

        self._build_ui()

    @staticmethod
    def _snap_to_half_hour(ts: float) -> int:
        dt = datetime.datetime.fromtimestamp(ts)
        minute = 0 if dt.minute < 30 else 30
        snapped = dt.replace(minute=minute, second=0, microsecond=0)
        return int(snapped.timestamp())

    def _build_ui(self) -> None:
        # --- Top Toolbar: Time Navigation + Search + Refresh ---
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("card-panel")

        btn_prev = Gtk.Button(label="◀ -2h")
        btn_prev.connect("clicked", lambda *_: self._shift_window(-7200))
        toolbar.append(btn_prev)

        btn_now = Gtk.Button(label="Now")
        btn_now.connect("clicked", lambda *_: self._jump_now())
        toolbar.append(btn_now)

        btn_next = Gtk.Button(label="+2h ▶")
        btn_next.connect("clicked", lambda *_: self._shift_window(7200))
        toolbar.append(btn_next)

        self.lbl_window_range = Gtk.Label(label="")
        self.lbl_window_range.add_css_class("heading")
        self.lbl_window_range.set_margin_start(8)
        self.lbl_window_range.set_margin_end(8)
        toolbar.append(self.lbl_window_range)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search shows, movies, sports, episodes...")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", lambda *_: self._rebuild_guide_content())
        toolbar.append(self.search_entry)

        self.btn_refresh = Gtk.Button(label="Refresh EPG")
        self.btn_refresh.set_icon_name("view-refresh-symbolic")
        self.btn_refresh.connect("clicked", self._on_refresh_epg)
        toolbar.append(self.btn_refresh)

        self.append(toolbar)

        # --- Middle: Scrollable EPG Timeline Grid / Search Results ---
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)

        self.grid_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.scrolled.set_child(self.grid_container)
        self.append(self.scrolled)

        # --- Bottom: Selected Program Inspector & Watch / Record Actions ---
        detail_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        detail_card.add_css_class("card-panel")

        top_detail = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.lbl_prog_title = Gtk.Label(label="Click any program in the guide to view details & record")
        self.lbl_prog_title.add_css_class("title-4")
        self.lbl_prog_title.set_halign(Gtk.Align.START)
        self.lbl_prog_title.set_hexpand(True)
        self.lbl_prog_title.set_ellipsize(Pango.EllipsizeMode.END)
        top_detail.append(self.lbl_prog_title)

        self.btn_watch_selected = Gtk.Button(label="Watch Channel")
        self.btn_watch_selected.set_icon_name("media-playback-start-symbolic")
        self.btn_watch_selected.set_sensitive(False)
        self.btn_watch_selected.connect("clicked", self._on_watch_selected)
        top_detail.append(self.btn_watch_selected)

        self.btn_record_selected = Gtk.Button(label="Record Program")
        self.btn_record_selected.set_icon_name("media-record-symbolic")
        self.btn_record_selected.add_css_class("destructive-action")
        self.btn_record_selected.set_sensitive(False)
        self.btn_record_selected.connect("clicked", self._on_record_selected)
        top_detail.append(self.btn_record_selected)

        detail_card.append(top_detail)

        self.lbl_prog_meta = Gtk.Label(label="")
        self.lbl_prog_meta.set_halign(Gtk.Align.START)
        self.lbl_prog_meta.add_css_class("caption")
        detail_card.append(self.lbl_prog_meta)

        self.lbl_prog_synopsis = Gtk.Label(label="")
        self.lbl_prog_synopsis.set_halign(Gtk.Align.START)
        self.lbl_prog_synopsis.set_wrap(True)
        self.lbl_prog_synopsis.add_css_class("dim-label")
        detail_card.append(self.lbl_prog_synopsis)

        self.append(detail_card)

    def set_device_and_channels(
        self, device: Optional[HDHomeRunDevice], channels: List[ChannelItem]
    ) -> None:
        self.device = device
        self.channels = channels
        self._rebuild_guide_content()

    def _shift_window(self, delta_sec: int) -> None:
        self.window_start_ts += delta_sec
        self._rebuild_guide_content()

    def _jump_now(self) -> None:
        self.window_start_ts = self._snap_to_half_hour(time.time())
        self._rebuild_guide_content()

    def _on_refresh_epg(self, _btn: Gtk.Button) -> None:
        self.btn_refresh.set_sensitive(False)
        self.on_toast("Refreshing program guide data...")

        def _worker() -> None:
            ok, msg = self.epg.refresh_epg(self.device)
            GLib.idle_add(self._after_refresh, ok, msg)

        threading.Thread(target=_worker, daemon=True).start()

    def _after_refresh(self, _ok: bool, msg: str) -> None:
        self.btn_refresh.set_sensitive(True)
        self.on_toast(msg)
        self._rebuild_guide_content()

    def _rebuild_guide_content(self) -> None:
        while True:
            child = self.grid_container.get_first_child()
            if not child:
                break
            self.grid_container.remove(child)

        win_start = self.window_start_ts
        win_end = win_start + (WINDOW_MINUTES * 60)

        st_dt = datetime.datetime.fromtimestamp(win_start)
        et_dt = datetime.datetime.fromtimestamp(win_end)
        self.lbl_window_range.set_text(
            f"{st_dt.strftime('%a %b %-d, %-I:%M %p')} – {et_dt.strftime('%-I:%M %p')}"
        )

        query = self.search_entry.get_text().strip()
        if query:
            self._build_search_results(query)
            return

        # 1. Timeline Header Row (30-min markers)
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ch_corner = Gtk.Label(label="Channel")
        ch_corner.set_size_request(175, 32)
        ch_corner.add_css_class("heading")
        header_row.append(ch_corner)

        slot_width = int(30 * PIXELS_PER_MINUTE)
        for i in range(WINDOW_MINUTES // 30):
            slot_ts = win_start + (i * 1800)
            slot_str = datetime.datetime.fromtimestamp(slot_ts).strftime("%-I:%M %p")
            lbl_slot = Gtk.Label(label=slot_str, halign=Gtk.Align.START)
            lbl_slot.set_size_request(slot_width, 32)
            lbl_slot.add_css_class("heading")
            lbl_slot.add_css_class("dim-label")
            header_row.append(lbl_slot)

        self.grid_container.append(header_row)

        # 2. Channel Rows
        active_channels = [
            c
            for c in self.channels
            if (not c.blocked or self.config.show_blocked_channels)
            and (c.subscribed or self.config.show_unsubscribed_channels)
        ]

        now_ts = time.time()
        for ch in active_channels:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

            # Channel cell
            ch_btn = Gtk.Button()
            ch_btn.set_size_request(175, 58)
            ch_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            badge = Gtk.Label(label=ch.guide_number)
            badge.add_css_class("channel-number-badge")
            ch_inner.append(badge)

            cname = Gtk.Label(label=ch.guide_name, halign=Gtk.Align.START, hexpand=True)
            cname.set_ellipsize(Pango.EllipsizeMode.END)
            cname.add_css_class("heading")
            ch_inner.append(cname)
            ch_btn.set_child(ch_inner)
            ch_btn.connect("clicked", lambda _b, item=ch: self.on_watch_channel(item))
            row_box.append(ch_btn)

            # Program slots in [win_start, win_end]
            progs = self.epg.get_programs_in_window(ch.guide_number, win_start, win_end)
            if not progs:
                empty_btn = Gtk.Button(label="No guide listings in this time window")
                empty_btn.set_size_request(int(WINDOW_MINUTES * PIXELS_PER_MINUTE), 58)
                empty_btn.add_css_class("epg-slot")
                empty_btn.add_css_class("dim-label")
                empty_Prog = ProgramEntry(
                    guide_number=ch.guide_number,
                    channel_name=ch.guide_name,
                    title=f"{ch.guide_name} Live Programming",
                    start_time=win_start,
                    end_time=win_start + 3600,
                )
                empty_btn.connect("clicked", self._on_program_clicked, empty_Prog)
                row_box.append(empty_btn)
            else:
                cursor = win_start
                for p in progs:
                    if p.start_time > cursor:
                        gap_mins = max(5, (p.start_time - cursor) / 60.0)
                        gap_lbl = Gtk.Label(label="")
                        gap_lbl.set_size_request(int(gap_mins * PIXELS_PER_MINUTE), 58)
                        row_box.append(gap_lbl)

                    vis_start = max(win_start, p.start_time)
                    vis_end = min(win_end, p.end_time)
                    vis_mins = max(12.0, (vis_end - vis_start) / 60.0)
                    px_width = max(70, int(vis_mins * PIXELS_PER_MINUTE) - 4)

                    p_btn = Gtk.Button()
                    p_btn.set_size_request(px_width, 58)

                    is_rec = self._is_program_scheduled_or_recording(p)
                    if is_rec:
                        p_btn.add_css_class("epg-slot-recording")
                    elif p.is_airing_at(now_ts):
                        p_btn.add_css_class("epg-slot-active")
                    else:
                        p_btn.add_css_class("epg-slot")

                    p_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                    p_vbox.set_valign(Gtk.Align.CENTER)

                    t_str = f"● REC {p.title}" if is_rec else p.title
                    tlbl = Gtk.Label(label=t_str, halign=Gtk.Align.START)
                    tlbl.set_ellipsize(Pango.EllipsizeMode.END)
                    tlbl.add_css_class("heading")
                    p_vbox.append(tlbl)

                    sub_str = p.episode_title or p.episode_number or p.time_range_str
                    slbl = Gtk.Label(label=sub_str, halign=Gtk.Align.START)
                    slbl.set_ellipsize(Pango.EllipsizeMode.END)
                    slbl.add_css_class("caption")
                    slbl.add_css_class("dim-label")
                    p_vbox.append(slbl)

                    p_btn.set_child(p_vbox)
                    p_btn.connect("clicked", self._on_program_clicked, p)
                    row_box.append(p_btn)
                    cursor = max(cursor, vis_end)

            self.grid_container.append(row_box)

    def _build_search_results(self, query: str) -> None:
        matches = self.epg.search_programs(query)
        header = Gtk.Label(
            label=f"Found {len(matches)} upcoming programs matching '{query}'",
            halign=Gtk.Align.START,
        )
        header.add_css_class("heading")
        self.grid_container.append(header)

        for p in matches[:80]:
            btn = Gtk.Button()
            btn.add_css_class("epg-slot")
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

            badge = Gtk.Label(label=f"{p.guide_number} {p.channel_name}")
            badge.add_css_class("channel-number-badge")
            hbox.append(badge)

            dt_str = datetime.datetime.fromtimestamp(p.start_time).strftime("%a %-I:%M %p")
            time_lbl = Gtk.Label(label=dt_str)
            time_lbl.add_css_class("mono-value")
            hbox.append(time_lbl)

            ep_part = f" — {p.episode_title}" if p.episode_title else ""
            title_lbl = Gtk.Label(
                label=f"{p.title}{ep_part}",
                halign=Gtk.Align.START,
                hexpand=True,
            )
            title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            title_lbl.add_css_class("heading")
            hbox.append(title_lbl)

            btn.set_child(hbox)
            btn.connect("clicked", self._on_program_clicked, p)
            self.grid_container.append(btn)

    def _is_program_scheduled_or_recording(self, prog: ProgramEntry) -> bool:
        for rec in self.config.recordings:
            if (
                rec.channel_number == prog.guide_number
                and abs(rec.start_time - prog.start_time) < 120
                and rec.status in ("scheduled", "recording")
            ):
                return True
        return False

    def _on_program_clicked(self, _btn: Gtk.Button, prog: ProgramEntry) -> None:
        self.selected_program = prog
        self.btn_watch_selected.set_sensitive(True)
        self.btn_record_selected.set_sensitive(True)

        ep_meta = []
        if prog.episode_number:
            ep_meta.append(prog.episode_number)
        if prog.episode_title:
            ep_meta.append(prog.episode_title)
        ep_suffix = f" — {' • '.join(ep_meta)}" if ep_meta else ""

        self.lbl_prog_title.set_text(
            f"[{prog.guide_number} {prog.channel_name}]  {prog.title}{ep_suffix}"
        )

        cats = ", ".join(prog.categories) if prog.categories else "General"
        date_str = datetime.datetime.fromtimestamp(prog.start_time).strftime("%A, %b %-d")
        self.lbl_prog_meta.set_text(
            f"{date_str}  •  {prog.time_range_str} ({prog.duration_minutes} min)  •  Category: {cats}"
        )
        self.lbl_prog_synopsis.set_text(
            prog.synopsis or "No synopsis provided for this broadcast."
        )

        if self._is_program_scheduled_or_recording(prog):
            self.btn_record_selected.set_label("Cancel Recording")
        elif prog.is_airing_at():
            self.btn_record_selected.set_label("Record Now")
        else:
            self.btn_record_selected.set_label("Schedule Recording")

    def _on_watch_selected(self, _btn: Gtk.Button) -> None:
        if not self.selected_program:
            return
        for ch in self.channels:
            if ch.guide_number == self.selected_program.guide_number:
                self.on_watch_channel(ch)
                return

    def _on_record_selected(self, _btn: Gtk.Button) -> None:
        prog = self.selected_program
        if not prog:
            return

        # If already scheduled/recording, cancel it
        for rec in self.config.recordings:
            if (
                rec.channel_number == prog.guide_number
                and abs(rec.start_time - prog.start_time) < 120
                and rec.status in ("scheduled", "recording")
            ):
                self.dvr.stop_or_cancel_recording(rec.id)
                self.on_toast(f"Cancelled recording for '{prog.title}'.")
                self._on_program_clicked(_btn, prog)
                self._rebuild_guide_content()
                return

        stream_url = ""
        for ch in self.channels:
            if ch.guide_number == prog.guide_number:
                stream_url = ch.url
                break
        if not stream_url and self.device:
            stream_url = f"http://{self.device.local_ip}:5004/auto/v{prog.guide_number}"

        rec = self.dvr.schedule_recording(
            title=prog.title,
            channel_number=prog.guide_number,
            channel_name=prog.channel_name,
            stream_url=stream_url,
            start_time=prog.start_time,
            end_time=prog.end_time,
            episode_title=prog.episode_title,
            episode_number=prog.episode_number,
            synopsis=prog.synopsis,
        )
        state_word = "Started recording" if rec.status == "recording" else "Scheduled recording"
        self.on_toast(f"{state_word}: {rec.title} ({prog.time_range_str})")
        self._on_program_clicked(_btn, prog)
        self._rebuild_guide_content()

