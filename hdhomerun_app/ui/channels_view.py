"""Channel Management View: Hardware Scan, Add Custom Channel, Block/Disable, Favorite, and Signal Preview."""

from __future__ import annotations

import threading
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from ..backend.client import ChannelItem, HDHomeRunClient, HDHomeRunDevice, ScanStatus
from ..config import AppConfig, CustomChannel


class ChannelsView(Gtk.Box):
    """Manage channels: scan hardware tuners, add custom channels, block/unblock, favorite."""

    def __init__(
        self,
        config: AppConfig,
        client: HDHomeRunClient,
        on_channels_changed: Callable[[], None],
        on_watch_channel: Callable[[ChannelItem], None],
        on_probe_signal: Callable[[str], None],
        on_toast: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(14)
        self.set_margin_end(14)

        self.config = config
        self.client = client
        self.on_channels_changed = on_channels_changed
        self.on_watch_channel = on_watch_channel
        self.on_probe_signal = on_probe_signal
        self.on_toast = on_toast

        self.device: Optional[HDHomeRunDevice] = None
        self.channels: List[ChannelItem] = []
        self._filter_mode: str = "all"  # all | active | fav | blocked | custom
        self._scan_polling: bool = False

        self._build_ui()

    def _build_ui(self) -> None:
        # --- Top Hardware Channel Scan & Add Channel Bar ---
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top_bar.add_css_class("card-panel")

        scan_icon = Gtk.Image.new_from_icon_name("network-wireless-acquiring-symbolic")
        scan_icon.set_pixel_size(24)
        top_bar.append(scan_icon)

        scan_title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_title = Gtk.Label(label="Tuner Channel Lineup & Scanner")
        lbl_title.set_halign(Gtk.Align.START)
        lbl_title.add_css_class("heading")
        scan_title_box.append(lbl_title)

        self.lbl_summary = Gtk.Label(label="0 channels loaded")
        self.lbl_summary.set_halign(Gtk.Align.START)
        self.lbl_summary.add_css_class("caption")
        self.lbl_summary.add_css_class("dim-label")
        scan_title_box.append(self.lbl_summary)
        top_bar.append(scan_title_box)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top_bar.append(spacer)

        # Signal source dropdown (Antenna / Cable)
        lbl_src = Gtk.Label(label="Source:")
        top_bar.append(lbl_src)

        self.source_combo = Gtk.DropDown.new_from_strings(["Antenna", "Cable"])
        self.source_combo.set_valign(Gtk.Align.CENTER)
        top_bar.append(self.source_combo)

        self.btn_scan = Gtk.Button(label="Scan Channels")
        self.btn_scan.set_icon_name("view-refresh-symbolic")
        self.btn_scan.add_css_class("suggested-action")
        self.btn_scan.connect("clicked", self._on_scan_clicked)
        top_bar.append(self.btn_scan)

        self.btn_add_custom = Gtk.Button(label="Add Channel")
        self.btn_add_custom.set_icon_name("list-add-symbolic")
        self.btn_add_custom.connect("clicked", self._on_add_custom_dialog)
        top_bar.append(self.btn_add_custom)

        self.append(top_bar)

        # --- Hardware Scan Progress Bar (Revealer) ---
        self.scan_revealer = Gtk.Revealer()
        self.scan_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)

        scan_prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scan_prog_box.add_css_class("status-banner")

        scan_prog_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lbl_scan_status = Gtk.Label(label="Scanning RF frequencies...")
        self.lbl_scan_status.set_halign(Gtk.Align.START)
        self.lbl_scan_status.set_hexpand(True)
        self.lbl_scan_status.add_css_class("heading")
        scan_prog_header.append(self.lbl_scan_status)

        self.btn_abort_scan = Gtk.Button(label="Stop Scan")
        self.btn_abort_scan.add_css_class("destructive-action")
        self.btn_abort_scan.connect("clicked", self._on_abort_scan_clicked)
        scan_prog_header.append(self.btn_abort_scan)
        scan_prog_box.append(scan_prog_header)

        self.scan_progress = Gtk.ProgressBar()
        self.scan_progress.set_show_text(True)
        scan_prog_box.append(self.scan_progress)

        self.scan_revealer.set_child(scan_prog_box)
        self.append(self.scan_revealer)

        # --- Search & Filter Row ---
        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search by channel number, callsign, or codec...")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", lambda *_: self._rebuild_rows())
        filter_row.append(self.search_entry)

        first_toggle: Optional[Gtk.ToggleButton] = None
        for label, mode in [
            ("All", "all"),
            ("Active", "active"),
            ("Favorites", "fav"),
            ("Blocked", "blocked"),
            ("Custom", "custom"),
        ]:
            tbtn = Gtk.ToggleButton(label=label)
            if first_toggle is None:
                first_toggle = tbtn
                tbtn.set_active(True)
            else:
                tbtn.set_group(first_toggle)
            tbtn.connect("toggled", self._on_filter_changed, mode)
            filter_row.append(tbtn)

        self.append(filter_row)

        # --- Channel List ---
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        scrolled.set_child(self.listbox)
        self.append(scrolled)

    def set_device_and_channels(self, device: Optional[HDHomeRunDevice], channels: List[ChannelItem]) -> None:
        self.device = device
        self.channels = channels
        if device and device.source_list:
            model = Gtk.StringList.new(device.source_list)
            self.source_combo.set_model(model)
            if device.source in device.source_list:
                self.source_combo.set_selected(device.source_list.index(device.source))
        self._update_summary()
        self._rebuild_rows()

    def _update_summary(self) -> None:
        total = len(self.channels)
        active = sum(1 for c in self.channels if not c.blocked and c.subscribed)
        favs = sum(1 for c in self.channels if c.favorite)
        blocked = sum(1 for c in self.channels if c.blocked)
        custom = sum(1 for c in self.channels if c.custom)
        self.lbl_summary.set_text(
            f"Total: {total}  •  Active: {active}  •  Favorites: {favs}  •  Blocked: {blocked}  •  Custom: {custom}"
        )

    def _on_filter_changed(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if btn.get_active():
            self._filter_mode = mode
            self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        while True:
            child = self.listbox.get_first_child()
            if not child:
                break
            self.listbox.remove(child)

        query = self.search_entry.get_text().strip().lower()

        for ch in self.channels:
            if not ch.subscribed and not self.config.show_unsubscribed_channels and self._filter_mode != "all":
                continue
            if self._filter_mode == "active" and (ch.blocked or not ch.subscribed):
                continue
            if self._filter_mode == "fav" and not ch.favorite:
                continue
            if self._filter_mode == "blocked" and not ch.blocked:
                continue
            if self._filter_mode == "custom" and not ch.custom:
                continue

            if query:
                hay = f"{ch.guide_number} {ch.guide_name} {ch.video_codec} {ch.audio_codec}".lower()
                if query not in hay:
                    continue

            self.listbox.append(self._create_channel_row(ch))

    def _create_channel_row(self, ch: ChannelItem) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hbox.set_margin_top(8)
        hbox.set_margin_bottom(8)
        hbox.set_margin_start(12)
        hbox.set_margin_end(12)

        # Channel Number
        num_badge = Gtk.Label(label=ch.guide_number)
        num_badge.set_size_request(62, -1)
        num_badge.add_css_class("channel-number-badge")
        hbox.append(num_badge)

        # Name & metadata column
        meta_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        meta_col.set_hexpand(True)

        title_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_lbl = Gtk.Label(label=ch.guide_name)
        name_lbl.add_css_class("heading")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_line.append(name_lbl)

        if ch.hd:
            hd_lbl = Gtk.Label(label="HD")
            hd_lbl.add_css_class("tag-hd")
            title_line.append(hd_lbl)

        if ch.favorite:
            fav_lbl = Gtk.Label(label="FAV")
            fav_lbl.add_css_class("tag-fav")
            title_line.append(fav_lbl)

        if ch.blocked:
            blk_lbl = Gtk.Label(label="BLOCKED")
            blk_lbl.add_css_class("tag-blocked")
            title_line.append(blk_lbl)

        if ch.custom:
            cust_lbl = Gtk.Label(label="CUSTOM")
            cust_lbl.add_css_class("tag-custom")
            title_line.append(cust_lbl)

        if not ch.subscribed:
            unsub_lbl = Gtk.Label(label="NO SIGNAL / UNSUB")
            unsub_lbl.add_css_class("tag-blocked")
            title_line.append(unsub_lbl)

        meta_col.append(title_line)

        codec_parts = []
        if ch.video_codec:
            codec_parts.append(f"Video: {ch.video_codec}")
        if ch.audio_codec:
            codec_parts.append(f"Audio: {ch.audio_codec}")
        codec_parts.append(f"URL: {ch.url}")
        sub_lbl = Gtk.Label(label="  •  ".join(codec_parts))
        sub_lbl.set_halign(Gtk.Align.START)
        sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        sub_lbl.add_css_class("caption")
        sub_lbl.add_css_class("dim-label")
        meta_col.append(sub_lbl)
        hbox.append(meta_col)

        # Signal badge from scan
        sig_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sig_box.set_valign(Gtk.Align.CENTER)
        sig_box.set_size_request(125, -1)
        ss_lbl = Gtk.Label(label=f"SS: {ch.signal_strength}%  SNQ: {ch.signal_quality}%")
        ss_lbl.add_css_class("caption")
        if ch.signal_quality >= 60:
            ss_lbl.add_css_class("signal-good")
        elif ch.signal_quality >= 35:
            ss_lbl.add_css_class("signal-warn")
        else:
            ss_lbl.add_css_class("signal-bad")
        sig_box.append(ss_lbl)

        sig_bar = Gtk.LevelBar()
        sig_bar.set_min_value(0.0)
        sig_bar.set_max_value(100.0)
        sig_bar.set_value(float(ch.signal_quality or ch.signal_strength))
        sig_box.append(sig_bar)
        hbox.append(sig_box)

        # Action Buttons: Favorite, Block/Unblock, Check Signal, Watch, Delete (if custom)
        btn_fav = Gtk.ToggleButton(label="★ Fav")
        btn_fav.set_active(ch.favorite)
        btn_fav.set_valign(Gtk.Align.CENTER)
        btn_fav.set_tooltip_text("Toggle Favorite on HDHomeRun & Desktop App")
        btn_fav.connect("toggled", self._on_toggle_favorite, ch)
        hbox.append(btn_fav)

        btn_block = Gtk.ToggleButton(label="Blocked" if ch.blocked else "Block")
        btn_block.set_active(ch.blocked)
        btn_block.set_valign(Gtk.Align.CENTER)
        btn_block.set_tooltip_text("Block/Disable or Unblock this channel")
        if ch.blocked:
            btn_block.add_css_class("destructive-action")
        btn_block.connect("toggled", self._on_toggle_block, ch)
        hbox.append(btn_block)

        btn_sig = Gtk.Button(label="Signal")
        btn_sig.set_icon_name("network-cellular-signal-good-symbolic")
        btn_sig.set_valign(Gtk.Align.CENTER)
        btn_sig.set_tooltip_text("Test Live RF Signal for this channel")
        btn_sig.connect("clicked", lambda *_: self.on_probe_signal(ch.guide_number))
        hbox.append(btn_sig)

        btn_watch = Gtk.Button(label="Watch")
        btn_watch.set_icon_name("media-playback-start-symbolic")
        btn_watch.set_valign(Gtk.Align.CENTER)
        btn_watch.connect("clicked", lambda *_: self.on_watch_channel(ch))
        hbox.append(btn_watch)

        if ch.custom:
            btn_del = Gtk.Button()
            btn_del.set_icon_name("user-trash-symbolic")
            btn_del.set_valign(Gtk.Align.CENTER)
            btn_del.set_tooltip_text("Remove custom channel")
            btn_del.add_css_class("destructive-action")
            btn_del.connect("clicked", self._on_delete_custom_channel, ch)
            hbox.append(btn_del)

        row.set_child(hbox)
        return row

    def _on_toggle_favorite(self, btn: Gtk.ToggleButton, ch: ChannelItem) -> None:
        new_fav = btn.get_active()
        ch.favorite = new_fav

        def _worker() -> None:
            if self.device:
                self.client.set_channel_favorite(self.device, ch.guide_number, new_fav)
            else:
                self.config.set_channel_favorite(ch.guide_number, new_fav)
            GLib.idle_add(self._after_channel_mutation, f"{'Favorited' if new_fav else 'Unfavorited'} {ch.guide_number} ({ch.guide_name})")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_toggle_block(self, btn: Gtk.ToggleButton, ch: ChannelItem) -> None:
        new_blocked = btn.get_active()
        ch.blocked = new_blocked

        def _worker() -> None:
            if self.device:
                self.client.set_channel_blocked(self.device, ch.guide_number, new_blocked)
            else:
                self.config.set_channel_blocked(ch.guide_number, new_blocked)
            GLib.idle_add(
                self._after_channel_mutation,
                f"{'Blocked' if new_blocked else 'Unblocked'} channel {ch.guide_number} ({ch.guide_name})",
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_delete_custom_channel(self, _btn: Gtk.Button, ch: ChannelItem) -> None:
        self.config.remove_custom_channel(ch.guide_number)
        self._after_channel_mutation(f"Removed custom channel {ch.guide_number}")

    def _after_channel_mutation(self, msg: str) -> None:
        self.on_toast(msg)
        self._update_summary()
        self._rebuild_rows()
        self.on_channels_changed()

    def _on_scan_clicked(self, _btn: Gtk.Button) -> None:
        if not self.device:
            self.on_toast("No HDHomeRun device discovered.")
            return
        selected_item = self.source_combo.get_selected_item()
        source = selected_item.get_string() if selected_item else "Antenna"

        def _start() -> None:
            if not self.device:
                return
            ok, msg = self.client.start_channel_scan(self.device, source=source)
            GLib.idle_add(self._on_scan_started, ok, msg)

        threading.Thread(target=_start, daemon=True).start()

    def _on_scan_started(self, ok: bool, msg: str) -> None:
        self.on_toast(msg)
        if ok:
            self.scan_revealer.set_reveal_child(True)
            self.btn_scan.set_sensitive(False)
            if not self._scan_polling:
                self._scan_polling = True
                GLib.timeout_add(1200, self._poll_scan_status)

    def _on_abort_scan_clicked(self, _btn: Gtk.Button) -> None:
        if not self.device:
            return

        def _abort() -> None:
            if not self.device:
                return
            ok, msg = self.client.abort_channel_scan(self.device)
            GLib.idle_add(self.on_toast, msg)

        threading.Thread(target=_abort, daemon=True).start()

    def _poll_scan_status(self) -> bool:
        if not self.device:
            self._scan_polling = False
            self.scan_revealer.set_reveal_child(False)
            self.btn_scan.set_sensitive(True)
            return False

        def _query() -> None:
            if not self.device:
                return
            st = self.client.get_scan_status(self.device)
            GLib.idle_add(self._update_scan_ui, st)

        threading.Thread(target=_query, daemon=True).start()
        return self._scan_polling

    def _update_scan_ui(self, st: ScanStatus) -> None:
        if st.scan_in_progress:
            frac = max(0.0, min(1.0, st.progress / 100.0))
            self.scan_progress.set_fraction(frac)
            self.scan_progress.set_text(f"{st.progress}% — {st.found} channels found")
            self.lbl_scan_status.set_text(
                f"Scanning {st.source}... ({st.progress}% complete, {st.found} channels found)"
            )
        else:
            self._scan_polling = False
            self.scan_revealer.set_reveal_child(False)
            self.btn_scan.set_sensitive(True)
            self.on_toast("Channel scan finished. Refreshing lineup...")
            self.on_channels_changed()

    def _on_add_custom_dialog(self, _btn: Gtk.Button) -> None:
        root = self.get_root()
        dialog = Gtk.Window(
            title="Add Custom / Manual Channel",
            transient_for=root if isinstance(root, Gtk.Window) else None,
            modal=True,
            default_width=440,
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)

        lbl_num = Gtk.Label(label="Channel Number:", halign=Gtk.Align.START)
        entry_num = Gtk.Entry(placeholder_text="e.g. 5.1 or 100.1", hexpand=True)
        grid.attach(lbl_num, 0, 0, 1, 1)
        grid.attach(entry_num, 1, 0, 1, 1)

        lbl_name = Gtk.Label(label="Channel Name:", halign=Gtk.Align.START)
        entry_name = Gtk.Entry(placeholder_text="e.g. PBS HD or Local Cam", hexpand=True)
        grid.attach(lbl_name, 0, 1, 1, 1)
        grid.attach(entry_name, 1, 1, 1, 1)

        default_ip = self.device.local_ip if self.device else "192.168.1.100"
        lbl_url = Gtk.Label(label="Stream URL:", halign=Gtk.Align.START)
        entry_url = Gtk.Entry(
            placeholder_text=f"http://{default_ip}:5004/auto/v5.1",
            hexpand=True,
        )
        grid.attach(lbl_url, 0, 2, 1, 1)
        grid.attach(entry_url, 1, 2, 1, 1)

        lbl_vcodec = Gtk.Label(label="Video Codec:", halign=Gtk.Align.START)
        entry_vcodec = Gtk.Entry(text="MPEG2", hexpand=True)
        grid.attach(lbl_vcodec, 0, 3, 1, 1)
        grid.attach(entry_vcodec, 1, 3, 1, 1)

        lbl_acodec = Gtk.Label(label="Audio Codec:", halign=Gtk.Align.START)
        entry_acodec = Gtk.Entry(text="AC3", hexpand=True)
        grid.attach(lbl_acodec, 0, 4, 1, 1)
        grid.attach(entry_acodec, 1, 4, 1, 1)

        chk_hd = Gtk.CheckButton(label="HD Channel", active=True)
        chk_fav = Gtk.CheckButton(label="Mark as Favorite", active=False)
        grid.attach(chk_hd, 0, 5, 1, 1)
        grid.attach(chk_fav, 1, 5, 1, 1)

        vbox.append(grid)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect("clicked", lambda *_: dialog.destroy())
        btn_save = Gtk.Button(label="Add Channel")
        btn_save.add_css_class("suggested-action")

        def _on_save(*_args: object) -> None:
            gnum = entry_num.get_text().strip()
            gname = entry_name.get_text().strip() or f"CH {gnum}"
            url = entry_url.get_text().strip()
            if not gnum:
                self.on_toast("Channel number is required.")
                return
            if not url:
                url = f"http://{default_ip}:5004/auto/v{gnum}"
            custom = CustomChannel(
                guide_number=gnum,
                guide_name=gname,
                url=url,
                video_codec=entry_vcodec.get_text().strip() or "MPEG2",
                audio_codec=entry_acodec.get_text().strip() or "AC3",
                hd=chk_hd.get_active(),
                favorite=chk_fav.get_active(),
                disabled=False,
            )
            self.config.add_or_update_custom_channel(custom)
            dialog.destroy()
            self._after_channel_mutation(f"Added channel {gnum} ({gname})")

        btn_save.connect("clicked", _on_save)
        btn_row.append(btn_cancel)
        btn_row.append(btn_save)
        vbox.append(btn_row)

        dialog.set_child(vbox)
        dialog.present()

