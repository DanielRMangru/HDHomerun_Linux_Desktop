# HDHomeRun Ubuntu Desktop (`hdhomerun-desktop`)

A robust, native **GTK4 + Libadwaita** GUI application for **Ubuntu Desktop** that provides complete control over **SiliconDust HDHomeRun** network TV tuners — including hardware channel scanning, adding and blocking channels, real-time RF signal metering, an interactive Electronic Program Guide (EPG), live TV playback with fullscreen & collapsible panels, and local DVR recording.

![Live TV Playback](screenshots/Screenshot%20From%202026-11-12%2012-12-54.png)

---

## Features & Screenshots

### 1. Live TV Playback with Fullscreen & Collapsible Panels
* **Embedded Player**: Built-in GStreamer (`gtk4paintablesink`) player directly inside the GTK4 window, plus one-click launch in **VLC** (`vlc`), `mpv`, or `ffplay`.
* **Collapsible Sidebar & Metadata**: Collapse or expand the channel selector sidebar (`Ctrl+B` or sidebar button) and the program details card for maximum viewing area.
* **Fullscreen & Theater Mode**: Press `F11`, double-click the video, or click the **Fullscreen** button to expand video across your entire display with a floating on-screen channel HUD.
* **Instant Recording**: Single-click to start recording the live broadcast immediately to local disk.

![Live TV Playback](screenshots/Screenshot%20From%202026-11-12%2012-12-54.png)

---

### 2. Electronic Program Guide (EPG)
* **Automatic Guide Fetching**: Slices multi-hour guide listings directly from the **SiliconDust HDHomeRun Guide API** (using device `DeviceAuth`) with local persistent caching and optional **XMLTV** file/URL support.
* **Interactive Timeline Grid**: Smooth horizontal timeline with `-2h`, `Now`, and `+2h` navigation markers.
* **Search & Program Details**: Full-text show/movie/sports search, detailed episode synopsis, original air date, and categories.
* **One-Click Scheduling**: Schedule upcoming airings directly into the local DVR scheduler from the program guide.

![Program Guide](screenshots/Screenshot%20From%202026-11-12%2012-13-00.png)

---

### 3. Channel Management (Scan, Add, Favorite, Block)
* **Hardware Channel Scanner**: Trigger and monitor hardware `Antenna` or `Cable` frequency scans directly on the HDHomeRun with real-time progress percentage and found channel counters.
* **Add Custom Channels**: Define custom virtual channels, subchannels, and manual stream URLs alongside hardware-detected channels.
* **Favorite & Block/Disable**: Toggle channel favorites (`★`) and block/disable unwanted channels. State synchronizes directly with the HDHomeRun hardware (`/lineup.post?favorite=x<ch>`) as well as local application views.
* **Search & Filter**: Filter channels instantly by All, Active, Favorites, Blocked, or Custom.

![Channels and Scanner](screenshots/Screenshot%20From%202026-11-12%2012-13-04.png)

---

### 4. Real-Time RF Signal Meter & Tuner Diagnostics
* **Live Hardware Telemetry**: Queries both HTTP `/status.json` and native TCP port `65001` control protocol (`/tunerN/status`, `/tunerN/vstatus`, `/tunerN/streaminfo`).
* **Visual Gauges & Rolling History Chart**: Real-time radial gauges and a 60-second time-series history graph for **Signal Strength (`SS`)**, **Signal-to-Noise Quality (`SNQ`)**, and **Symbol Error Quality (`SEQ`)**, along with carrier frequency (MHz), modulation lock status (`8vsb`, `qam`), and stream bitrate (Mbps).
* **Active Signal Probe**: Lock onto any virtual channel or raw RF frequency while adjusting or aiming your antenna.
* **Sweep All Channels**: Automatically sweep through every channel in your lineup to measure and benchmark live RF signal quality across your entire channel list.

![Signal Meter](screenshots/Screenshot%20From%202026-11-12%2012-13-09.png)

---

### 5. Local DVR & Recording Scheduler
* **Scheduled & Instant Recording**: Capture live TV or scheduled upcoming guide programs to local storage (`~/Videos/HDHomeRun` by default) in MPEG-TS (`.ts`) or `.mp4` format (using `ffmpeg -c copy`).
* **Timer Management**: View active recordings, scheduled upcoming timers, and completed recordings with file sizes, timestamps, and playback options.
* **Manual Timers**: Add custom timer recordings with custom channel selection, delay, and duration.
* **Headless Background Daemon**: Run the DVR scheduler service in the background using `./run.sh --daemon`.

![DVR Recordings](screenshots/Screenshot%20From%202026-11-12%2012-13-12.png)

---

### 6. Settings & Device Configuration
* **Hardware Information**: View discovered tuner details including friendly name, model number, device ID, firmware version, and tuner count.
* **Device Discovery & Manual IP**: Automatic UDP broadcast discovery on port `65001`, SiliconDust local HTTP discovery, or specify a manual device IP.
* **Storage & Padding Preferences**: Customize recordings directory, container format (`ts` / `mp4`), ffmpeg remuxing, pre-roll padding, and post-roll padding.
* **EPG & Playback Preferences**: Configure EPG source (SiliconDust API, XMLTV file/URL, or both) and default external player (`vlc`, `mpv`, `ffplay`).

![Settings](screenshots/Screenshot%20From%202026-11-12%2012-13-16.png)

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
