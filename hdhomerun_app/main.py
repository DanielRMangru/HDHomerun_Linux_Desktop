"""Main entry point for HDHomeRun Ubuntu Desktop Application (GUI + CLI diagnostic modes)."""

from __future__ import annotations

import argparse
import signal
import sys
import time

from . import __app_id__, __version__
from .backend.client import HDHomeRunClient
from .backend.epg import EPGManager
from .backend.recorder import DVRManager
from .config import AppConfig


def run_cli_status(config: AppConfig) -> int:
    """Print discovered HDHomeRun devices, tuner status, and lineup summary."""
    client = HDHomeRunClient(config)
    devices = client.discover_devices()
    if not devices:
        print("No HDHomeRun devices found on the network.")
        return 1

    for dev in devices:
        print(f"=== {dev.display_title} ===")
        print(f"  Base URL : {dev.base_url}")
        print(f"  Firmware : {dev.firmware_name} ({dev.firmware_version})")
        print(f"  Tuners   : {dev.tuner_count}  |  Source: {dev.source}")

        tuners = client.get_tuners_telemetry(dev)
        for t in tuners:
            state = f"ACTIVE ({t.vct_number} {t.vct_name})" if t.in_use else "IDLE"
            print(
                f"  [{t.resource}] {state} | Lock: {t.lock} | Freq: {t.frequency_mhz_str} | "
                f"SS: {t.signal_strength}% SNQ: {t.signal_quality}% SEQ: {t.symbol_quality}% | "
                f"{t.bitrate_mbps_str}"
            )

        lineup = client.get_lineup(dev, show_all=True)
        active = [c for c in lineup if not c.blocked and c.subscribed]
        favs = [c for c in lineup if c.favorite]
        blocked = [c for c in lineup if c.blocked]
        print(
            f"  Lineup   : {len(lineup)} total, {len(active)} active, "
            f"{len(favs)} favorites, {len(blocked)} blocked"
        )
    return 0


def run_cli_probe(config: AppConfig, channel: str) -> int:
    """Probe a specific channel's live RF signal and print results."""
    client = HDHomeRunClient(config)
    devices = client.discover_devices()
    if not devices:
        print("No HDHomeRun devices found.")
        return 1
    dev = devices[0]
    print(f"Probing channel {channel} on {dev.display_title} ...")
    telemetry = client.quick_probe_channel_signal(dev, channel, dwell_seconds=2.0)
    if not telemetry:
        print("Could not acquire tuner telemetry.")
        return 1
    print(f"  Tuner           : {telemetry.resource}")
    print(f"  Virtual Channel : {telemetry.vct_number} {telemetry.vct_name}")
    print(f"  Frequency       : {telemetry.frequency_mhz_str}")
    print(f"  Modulation Lock : {telemetry.lock}")
    print(f"  Signal Strength : {telemetry.signal_strength}%")
    print(f"  SNR Quality     : {telemetry.signal_quality}%")
    print(f"  Symbol Quality  : {telemetry.symbol_quality}%")
    print(f"  Stream Bitrate  : {telemetry.bitrate_mbps_str} ({telemetry.packets_per_sec} pps)")
    if telemetry.programs:
        print(f"  Mux Subchannels : {', '.join(telemetry.programs)}")
    return 0


def run_dvr_daemon(config: AppConfig) -> int:
    """Run headless DVR recording scheduler loop."""
    dvr = DVRManager(config)
    dvr.start_scheduler()
    print(f"HDHomeRun DVR Daemon started. Saving recordings to: {config.recordings_dir}")
    stop = False

    def _sig_handler(*_args: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    while not stop:
        time.sleep(1.0)
    dvr.stop_scheduler()
    return 0


def run_gui(config: AppConfig) -> int:
    """Launch the GTK4 + Libadwaita Ubuntu Desktop interface."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio

    from .ui.window import MainWindow

    class HDHomeRunApplication(Adw.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id=__app_id__,
                flags=Gio.ApplicationFlags.NON_UNIQUE,
            )
            self.window = None

        def do_activate(self) -> None:
            if not self.window:
                self.window = MainWindow(self, config)
            self.window.present()

    app = HDHomeRunApplication()
    return app.run(None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HDHomeRun Ubuntu Desktop UI — Channel Scanner, Signal Meter, EPG, Live TV & DVR"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Discover HDHomeRun hardware on LAN and print tuner & lineup status",
    )
    parser.add_argument(
        "--probe",
        metavar="CHANNEL",
        help="Probe live RF signal for a channel (e.g. 3.1) and print signal metrics",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run background DVR scheduler without opening a desktop window",
    )
    args = parser.parse_args()

    config = AppConfig()
    if args.status:
        return run_cli_status(config)
    if args.probe:
        return run_cli_probe(config, args.probe)
    if args.daemon:
        return run_dvr_daemon(config)
    return run_gui(config)


if __name__ == "__main__":
    sys.exit(main())

