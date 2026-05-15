# GTK + GStreamer NDI Player

Desktop player that:

- Decodes a URI with **`uridecodebin3`**
- Previews video in a **GTK 4** window (`gtk4paintablesink` → `Gtk.Video`, or `gtksink` / `glimagesink` fallback)
- Sends A/V to **NDI** from a **second pipeline** that stays in **PLAYING**, bridged with **`intervideosink` / `interaudiosrc`** and **`interaudiosink` / `interaudiosrc`** on channels `gtk_ndi_player_video` / `gtk_ndi_player_audio` (see [`app/gst_utils.py`](app/gst_utils.py))

## Requirements

- **Python 3.10+**
- **GStreamer 1.x** with Python bindings via **PyGObject**
- **GTK 4** and **GStreamer GTK sinks** for preview (`gtk4paintablesink` or `gtksink`)
- **NDI GStreamer plugin** providing `ndisink` (plugin name and pad layout vary by vendor build)

### Windows (pip wheels)

`pip install -r requirements.txt` installs **gstreamer-bundle** (GTK, GStreamer, and bundled `gi` / PyGObject). For NDI you still need an `ndisink` plugin and runtime that work in that environment (`gst-inspect-1.0 ndisink`).

### Windows (MSYS2 / gstreamer.dev)

Alternatively install GStreamer, GTK 4, PyGObject, and your NDI plugin in that stack; ensure `gst-inspect-1.0 ndisink` matches the Python environment you use.

### Linux

Install distro packages for `gstreamer1.0-plugins-base`, `gstreamer1.0-plugins-good`, GTK 4, GObject introspection, PyGObject, and your NDI plugin package.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python -m app
```

or

```bash
python app/main.py
```

## Controls

- **Stream URL** — HTTP(S) HLS/VOD, `file://`, `rtsp://`, etc.
- **Play / Pause / Stop** — transport for the **main** playback pipeline only; the **NDI** pipeline keeps running for the app lifetime.
- **Timeline slider** — scrub when the stream reports a finite duration and is seekable (many live HLS streams are not seekable).
- **Go to time** — seek to `H:MM:SS.mmm`, `MM:SS`, or seconds (e.g. `90.5`). Uses an accurate seek (`ACCURATE`) for this control.

## Troubleshooting

- **`Missing GStreamer plugins`** — install the indicated elements; URI decode requires **`uridecodebin3`** (`gst-inspect-1.0 uridecodebin3`).
- **`NDI output unavailable`** — `gst-inspect-1.0 ndisinkcombiner` and `gst-inspect-1.0 ndisink` must succeed. The NDI path is `inter* → ndisinkcombiner → ndisink` (see your working `gst-launch-1.0` with `combiner ! ndisink` and `! combiner.audio`).
- **No video** — ensure `gst-inspect-1.0 gtk4paintablesink` or `gtksink` or `glimagesink` exists.
