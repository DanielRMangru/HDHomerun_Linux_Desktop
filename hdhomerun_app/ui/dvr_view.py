"""Local DVR Recordings & Scheduled Timers View."""

from __future__ import annotations

import datetime
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Pango

from ..backend.client import ChannelItem, HDHomeRunDevice
from ..backend.recorder import DVRManager
from ..config import AppConfig, ScheduledRecording


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


class DVRView(Gtk.Box):
    """Manage active recordings, scheduled timers, and completed local video files."""

    def __init__(
        self,
        config: AppConfig,
        dvr: DVRManager,
        on_toast: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(14)
        self.set_margin_end(14)

        self.config = config
        self.dvr = dvr
        self.on_toast = on_toast

        self.device: Optional[HDHomeRunDevice] = None
        self.channels: List[ChannelItem] = []
        self._filter_mode: str = "all"

        self._build_ui()
        self.dvr.add_listener(lambda: GLib.idle_add(self.refresh_list))

    def _build_ui(self) -> None:
        # --- Header Card ---
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top_bar.add_css_class("card-panel")

        icon = Gtk.Image.new_from_icon_name("media-record-symbolic")
        icon.set_pixel_size(24)
        top_bar.append(icon)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Local DVR & Scheduled Recordings", halign=Gtk.Align.START)
        title.add_css_class("heading")
        meta_box.append(title)

        self.lbl_folder_summary = Gtk.Label(
            label=f"Storage Folder: {self.config.recordings_dir}",
            halign=Gtk.Align.START,
        )
        self.lbl_folder_summary.add_css_class("caption")
        self.lbl_folder_summary.add_css_class("dim-label")
        meta_box.append(self.lbl_folder_summary)
        top_bar.append(meta_box)

        spacer = Gtk.Box(hexpand=True)
        top_bar.append(spacer)

        btn_new = Gtk.Button(label="New Recording Timer")
        btn_new.set_icon_name("list-add-symbolic")
        btn_new.add_css_class("suggested-action")
        btn_new.connect("clicked", self._on_new_manual_recording)
        top_bar.append(btn_new)

        btn_open_dir = Gtk.Button(label="Open Folder")
        btn_open_dir.set_icon_name("folder-open-symbolic")
        btn_open_dir.connect("clicked", self._on_open_folder)
        top_bar.append(btn_open_dir)

        self.append(top_bar)

        # --- Filter Tabs ---
        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        first_btn: Optional[Gtk.ToggleButton] = None
        for label, mode in [
            ("All", "all"),
            ("Active & Scheduled", "upcoming"),
            ("Completed", "completed"),
            ("Failed / Cancelled", "failed"),
        ]:
            tbtn = Gtk.ToggleButton(label=label)
            if first_btn is None:
                first_btn = tbtn
                tbtn.set_active(True)
            else:
                tbtn.set_group(first_btn)
            tbtn.connect("toggled", self._on_filter_toggled, mode)
            filter_row.append(tbtn)
        self.append(filter_row)

        # --- Recordings List ---
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        scrolled.set_child(self.listbox)
        self.append(scrolled)

        self.refresh_list()

    def set_device_and_channels(
        self, device: Optional[HDHomeRunDevice], channels: List[ChannelItem]
    ) -> None:
        self.device = device
        self.channels = channels
        self.refresh_list()

    def _on_filter_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if btn.get_active():
            self._filter_mode = mode
            self.refresh_list()

    def refresh_list(self) -> None:
        self.lbl_folder_summary.set_text(
            f"Storage Folder: {self.config.recordings_dir}   •   "
            f"Active: {sum(1 for r in self.config.recordings if r.status == 'recording')}   •   "
            f"Scheduled: {sum(1 for r in self.config.recordings if r.status == 'scheduled')}   •   "
            f"Completed: {sum(1 for r in self.config.recordings if r.status == 'completed')}"
        )

        while True:
            child = self.listbox.get_first_child()
            if not child:
                break
            self.listbox.remove(child)

        for rec in self.config.recordings:
            if self._filter_mode == "upcoming" and rec.status not in ("recording", "scheduled"):
                continue
            if self._filter_mode == "completed" and rec.status != "completed":
                continue
            if self._filter_mode == "failed" and rec.status not in ("failed", "cancelled"):
                continue
            self.listbox.append(self._create_recording_row(rec))

    def _create_recording_row(self, rec: ScheduledRecording) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox.set_margin_top(10)
        hbox.set_margin_bottom(10)
        hbox.set_margin_start(12)
        hbox.set_margin_end(12)

        # Status Badge
        st_lbl = Gtk.Label(label=rec.status.upper())
        st_lbl.set_size_request(95, -1)
        st_lbl.set_valign(Gtk.Align.CENTER)
        if rec.status == "recording":
            st_lbl.add_css_class("tag-rec")
        elif rec.status == "completed":
            st_lbl.add_css_class("tag-hd")
        elif rec.status == "scheduled":
            st_lbl.add_css_class("tag-fav")
        else:
            st_lbl.add_css_class("tag-blocked")
        hbox.append(st_lbl)

        # Channel Badge
        ch_lbl = Gtk.Label(label=f"{rec.channel_number} {rec.channel_name}")
        ch_lbl.set_valign(Gtk.Align.CENTER)
        ch_lbl.add_css_class("channel-number-badge")
        hbox.append(ch_lbl)

        # Details column
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        col.set_hexpand(True)

        ep_str = f" ({rec.episode_number})" if rec.episode_number else ""
        ep_title = f" — {rec.episode_title}" if rec.episode_title else ""
        title_lbl = Gtk.Label(
            label=f"{rec.title}{ep_str}{ep_title}",
            halign=Gtk.Align.START,
        )
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.add_css_class("heading")
        col.append(title_lbl)

        st_dt = datetime.datetime.fromtimestamp(rec.start_time).strftime("%a %b %-d, %-I:%M %p")
        et_dt = datetime.datetime.fromtimestamp(rec.end_time).strftime("%-I:%M %p")
        size_str = _format_bytes(rec.bytes_written) if rec.bytes_written > 0 else "0 B"
        meta_lbl = Gtk.Label(
            label=f"{st_dt} – {et_dt}   •   Size: {size_str}   •   File: {rec.output_path}",
            halign=Gtk.Align.START,
        )
        meta_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        meta_lbl.add_css_class("caption")
        meta_lbl.add_css_class("dim-label")
        col.append(meta_lbl)

        if rec.error_message:
            err_lbl = Gtk.Label(label=f"Error: {rec.error_message}", halign=Gtk.Align.START)
            err_lbl.add_css_class("caption")
            err_lbl.add_css_class("signal-bad")
            col.append(err_lbl)

        hbox.append(col)

        # Action Buttons
        if rec.bytes_written > 0 and Path(rec.output_path).exists():
            btn_play = Gtk.Button(label="Play")
            btn_play.set_icon_name("media-playback-start-symbolic")
            btn_play.set_valign(Gtk.Align.CENTER)
            btn_play.connect("clicked", lambda _b, r=rec: self._play_recording_file(r))
            hbox.append(btn_play)

        if rec.status in ("recording", "scheduled"):
            btn_stop = Gtk.Button(
                label="Stop" if rec.status == "recording" else "Cancel"
            )
            btn_stop.add_css_class("destructive-action")
            btn_stop.set_valign(Gtk.Align.CENTER)
            btn_stop.connect(
                "clicked",
                lambda _b, rid=rec.id: self.dvr.stop_or_cancel_recording(rid),
            )
            hbox.append(btn_stop)

        btn_del = Gtk.Button()
        btn_del.set_icon_name("user-trash-symbolic")
        btn_del.set_tooltip_text("Delete recording entry and file")
        btn_del.set_valign(Gtk.Align.CENTER)
        btn_del.connect(
            "clicked",
            lambda _b, rid=rec.id: self._on_delete_recording(rid),
        )
        hbox.append(btn_del)

        row.set_child(hbox)
        return row

    def _play_recording_file(self, rec: ScheduledRecording) -> None:
        for player in [self.config.external_player, "vlc", "mpv", "ffplay", "xdg-open"]:
            if not player:
                continue
            bin_path = shutil.which(player)
            if bin_path:
                subprocess.Popen(
                    [bin_path, rec.output_path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.on_toast(f"Playing {Path(rec.output_path).name} in {player}")
                return
        self.on_toast("Could not find a media player (vlc/mpv) to play recording.")

    def _on_delete_recording(self, rec_id: str) -> None:
        self.dvr.delete_recording(rec_id, delete_file=True)
        self.on_toast("Deleted recording.")

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        folder = Path(self.config.recordings_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        xdg = shutil.which("xdg-open")
        if xdg:
            subprocess.Popen(
                [xdg, str(folder)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _on_new_manual_recording(self, _btn: Gtk.Button) -> None:
        root = self.get_root()
        dialog = Gtk.Window(
            title="New Manual / Instant Recording",
            transient_for=root if isinstance(root, Gtk.Window) else None,
            modal=True,
            default_width=430,
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)

        active_channels = [
            c for c in self.channels if not c.blocked and c.subscribed
        ] or self.channels

        ch_labels = [f"{c.guide_number} — {c.guide_name}" for c in active_channels] or ["3.1 — KCRA-TV"]
        combo_ch = Gtk.DropDown.new_from_strings(ch_labels)
        combo_ch.set_hexpand(True)
        grid.attach(Gtk.Label(label="Channel:", halign=Gtk.Align.START), 0, 0, 1, 1)
        grid.attach(combo_ch, 1, 0, 1, 1)

        entry_title = Gtk.Entry(placeholder_text="Recording Title (optional)", hexpand=True)
        grid.attach(Gtk.Label(label="Title:", halign=Gtk.Align.START), 0, 1, 1, 1)
        grid.attach(entry_title, 1, 1, 1, 1)

        spin_delay = Gtk.SpinButton.new_with_range(0, 1440, 5)
        spin_delay.set_value(0)
        grid.attach(
            Gtk.Label(label="Start In (minutes, 0 = Now):", halign=Gtk.Align.START),
            0,
            2,
            1,
            1,
        )
        grid.attach(spin_delay, 1, 2, 1, 1)

        spin_dur = Gtk.SpinButton.new_with_range(1, 360, 5)
        spin_dur.set_value(30)
        grid.attach(
            Gtk.Label(label="Duration (minutes):", halign=Gtk.Align.START),
            0,
            3,
            1,
            1,
        )
        grid.attach(spin_dur, 1, 3, 1, 1)

        vbox.append(grid)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect("clicked", lambda *_: dialog.destroy())

        btn_start = Gtk.Button(label="Schedule / Start Recording")
        btn_start.add_css_class("suggested-action")

        def _on_confirm(*_args: object) -> None:
            idx = combo_ch.get_selected()
            if active_channels and 0 <= idx < len(active_channels):
                ch = active_channels[idx]
                gnum = ch.guide_number
                gname = ch.guide_name
                url = ch.url
            else:
                gnum = "3.1"
                gname = "Channel 3.1"
                ip = self.device.local_ip if self.device else "192.168.1.100"
                url = f"http://{ip}:5004/auto/v3.1"

            delay_min = int(spin_delay.get_value())
            dur_min = int(spin_dur.get_value())
            start_ts = time.time() + (delay_min * 60)
            end_ts = start_ts + (dur_min * 60)
            title = entry_title.get_text().strip() or f"{gname} ({gnum})"

            rec = self.dvr.schedule_recording(
                title=title,
                channel_number=gnum,
                channel_name=gname,
                stream_url=url,
                start_time=start_ts,
                end_time=end_ts,
                pre_padding_sec=0 if delay_min == 0 else None,
            )
            dialog.destroy()
            self.on_toast(
                f"{'Recording started' if rec.status == 'recording' else 'Scheduled'}: {rec.title}"
            )

        btn_start.connect("clicked", _on_confirm)
        btn_row.append(btn_cancel)
        btn_row.append(btn_start)
        vbox.append(btn_row)

        dialog.set_child(vbox)
        dialog.present()

