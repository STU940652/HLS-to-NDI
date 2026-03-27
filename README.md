# HLS → NDI Player

A desktop application that ingests an HLS stream or VOD URL, displays it in a
window, and simultaneously outputs it as an NDI source — even while paused
(freeze on last frame).

Built with **Python 3**, **PyQt6**, and **GStreamer** (gst-ndi plugin).

---

## Requirements

### System packages

| Package | Notes |
|---|---|
| GStreamer 1.x | Core runtime |
| gst-plugins-good | `souphttpsrc`, `videoconvert`, etc. |
| gst-plugins-bad | `hlsdemux`, `glsinkbin`, `videoscale` |
| gst-plugins-ugly | H.264 decoder fallback |
| gst-libav / gst-plugins-ffmpeg | `avdec_h264`, wide codec support |
| **gst-plugin-ndi** | `ndisink` — see note below |
| NDI SDK runtime | Must be installed separately |

#### Ubuntu / Debian
```bash
sudo apt install \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  python3-gi python3-gst-1.0
```

#### macOS (Homebrew)
```bash
brew install gstreamer gst-plugins-good gst-plugins-bad \
             gst-plugins-ugly gst-libav
```

#### Windows
Use the official GStreamer MSVC installer from https://gstreamer.freedesktop.org/

---

### gst-plugin-ndi (ndisink)

The `ndisink` GStreamer element comes from the community plugin:

```
https://github.com/teltek/gst-plugin-ndi
```

You must also install the **NDI SDK** runtime from NewTek/Vizrt:
```
https://ndi.video/for-developers/ndi-sdk/
```

After installing the SDK, build and install the plugin:
```bash
git clone https://github.com/teltek/gst-plugin-ndi
cd gst-plugin-ndi
cargo build --release
cp target/release/libgstndi.so \
   $(pkg-config --variable=pluginsdir gstreamer-1.0)/
```

Verify it's found:
```bash
gst-inspect-1.0 ndisink
```

---

### Python packages

```bash
pip install PyQt6 PyGObject
```

> On Linux, `PyGObject` is often better installed via the system package
> manager: `sudo apt install python3-gi python3-gi-cairo`.

---

## Running

```bash
python hls_ndi_player.py
```

---

## Usage

1. Paste an HLS (`*.m3u8`) or direct VOD URL into the URL bar and press **LOAD**.
2. The stream starts playing automatically and appears in the video area.
3. The NDI source named **`HLS-NDI-Player`** is immediately visible to any NDI
   receiver on your local network (NDI Tools, vMix, OBS, Resolume, etc.).
4. Press **PAUSE** — playback stops but NDI continues broadcasting the last
   decoded frame (freeze-frame).
5. While paused, set the **START TC** field (HH:mm:ss.zzz) and press **SET**
   to jump to that timecode.
6. Press **PLAY** to resume.

---

## Architecture

```
souphttpsrc ──► decodebin ──► videoconvert ──► videoscale ──► tee ─┬─► queue ──► glsinkbin   (window)
                                                                     └─► queue ──► ndisink     (NDI)
```

- `souphttpsrc` handles HTTP/HTTPS, including chunked HLS playlists.
- `decodebin` auto-selects the right demuxer and decoder for `.m3u8` (via
  `hlsdemux`) and plain `.ts` / `.mp4` VOD files.
- A `tee` element splits the decoded video into two queued branches so the
  display and NDI sinks run independently.
- When GStreamer is in `PAUSED` state, both sinks retain the last pushed buffer,
  so NDI receivers see a frozen frame rather than signal loss.

---

## Customising the NDI source name

Edit the constant near the top of `hls_ndi_player.py`:

```python
NDI_SOURCE_NAME = "HLS-NDI-Player"
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ndisink` not found | Build & install gst-plugin-ndi; verify with `gst-inspect-1.0 ndisink` |
| Black video window | Try replacing `glsinkbin` with `autovideosink` in the pipeline code |
| No audio | Audio branch is not wired by design; add `audioconvert ! autoaudiosink` in `_on_pad_added` for audio |
| HLS playlist errors | Some CDNs block non-browser user-agents; set `src.set_property("user-agent", …)` |
| Seek bar stays grey | Stream is live HLS with no duration — seeking is unavailable for live streams |
