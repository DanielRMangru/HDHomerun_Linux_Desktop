# HDHomeRun Ubuntu Desktop (`hdhomerun-desktop`)

A robust, native **GTK4 + Libadwaita** GUI application for **Ubuntu Desktop** that provides complete control over **SiliconDust HDHomeRun** network TV tuners — including hardware channel scanning, adding and blocking channels, real-time RF signal metering, an interactive Electronic Program Guide (EPG), live TV playback, and local DVR recording.

---

## Features

1. **Automatic Network Discovery & Native Control Protocol**
   - Automatically discovers HDHomeRun devices on your local network via native UDP broadcast on port `65001` (`libhdhomerun` binary TLV protocol), SiliconDust local HTTP discovery, or manual IP configuration.
   - Displays hardware model, firmware version, device ID, tuner count, and active signal source.

2. **Channel Management (Scan, Add, Favorite, Block)**
   - **Hardware Channel Scanner**: Trigger and monitor `Antenna` or `Cable` frequency scans directly on the HDHomeRun with a real-time progress bar.
   - **Add Custom Channels**: Define custom virtual channels or stream URLs alongside hardware-detected channels.
   - **Block / Disable & Favorite**: Block unwanted channels (synced both to HDHomeRun hardware via `/lineup.post?favorite=x<ch>` and local filters) and mark Favorites (`★`).

3. **Real-Time RF Signal Meter & Tuner Diagnostics**
   - Queries both HTTP `/status.json` and native TCP port `65001` (`/tunerN/status`, `/tunerN/vstatus`, `/tunerN/streaminfo`).
   - Renders live Cairo radial gauges and a 60-second rolling time-series chart for **Signal Strength (`SS`)**, **Signal-to-Noise Quality (`SNQ`)**, and **Symbol Error Quality (`SEQ`)**, plus carrier frequency (MHz), stream bitrate (Mbps), and transport stream subchannel mux listings.
   - **Active Signal Probe & Full Lineup Sweep**: Lock any virtual channel or raw RF channel (`auto:24`) while aiming your antenna, or sweep all active channels sequentially.

4. **Electronic Program Guide (EPG)**
   - Automatically fetches multi-hour guide listings from the **SiliconDust HDHomeRun Guide API** (using the device's `DeviceAuth`) with local caching and optional **XMLTV** file/URL fallback.
   - Interactive horizontal timeline grid, keyword/category search, program synopses, and one-click **Watch Channel** or **Record Program**.

5. **Live TV Playback (Embedded + External)**
   - Built-in **GStreamer (`gtk4paintablesink`)** live TV player directly inside the GTK4 window, plus one-click launch in **VLC** (`vlc`), `mpv`, or `ffplay`.

6. **Local DVR & Recording Scheduler**
   - Record live channels immediately or schedule upcoming programs from the EPG or Manual Timer dialog to local disk (`~/Videos/HDHomeRun` by default) as MPEG-TS (`.ts`) or `.mp4` (via `ffmpeg -c copy`).
   - Includes a background scheduler thread and a headless `--daemon` CLI mode.

---

## Ubuntu System Requirements

Install the standard GTK4, Libadwaita, GStreamer, and media packages on Ubuntu:

```bash
sudo apt update
sudo apt install -y \
  python3 \
  python3-gi \
  python3-gi-cairo \
  python3-requests \
  gir1.2-gtk-4.0 \
  gir1.2-adw-1 \
  gir1.2-gstreamer-1.0 \
  gstreamer1.0-gtk4 \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  ffmpeg \
  vlc
```

---

## Quick Start

```bash
git clone https://github.com/DanielRMangru/HDHomerun_Linux_Desktop.git
cd HDHomerun_Linux_Desktop

# Launch the desktop UI
chmod +x run.sh install_desktop.sh
./run.sh
```

### Install Ubuntu Application Menu Shortcut

```bash
./install_desktop.sh
```

### CLI Diagnostic & Headless Modes

```bash
# Check discovered HDHomeRun devices, tuner telemetry, and lineup summary
./run.sh --status

# Probe live RF signal on a specific channel (e.g., 3.1)
./run.sh --probe 3.1

# Run headless DVR recording scheduler daemon
./run.sh --daemon
```

---

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```
