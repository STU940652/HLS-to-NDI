#!/usr/bin/env python3
"""
HLS/VOD → NDI Player
Built with PyQt6 + GStreamer (gst-ndi plugin)

Requirements:
    pip install PyQt6
    GStreamer with plugins: gst-plugins-good, gst-plugins-bad, gst-plugins-ugly
    gst-ndi plugin installed and NDI SDK runtime present

Usage:
    python hls_ndi_player.py
"""

import sys
import re
import threading
import time

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QSlider, QFrame, QSizePolicy,
    QTimeEdit, QMessageBox, QStatusBar,
)
from PyQt6.QtCore import Qt, QTimer, QTime, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QFont, QPalette, QColor

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo, GLib

Gst.init(None)


# ── Helpers ─────────────────────────────────────────────────────────────────

def ns_to_qtime(ns: int) -> QTime:
    ms = ns // 1_000_000
    h  = ms // 3_600_000;  ms %= 3_600_000
    m  = ms //    60_000;  ms %=    60_000
    s  = ms //     1_000;  ms %=     1_000
    return QTime(h, m, s, ms)

def qtime_to_ns(qt: QTime) -> int:
    return (
        qt.hour()   * 3_600_000_000_000
      + qt.minute() *    60_000_000_000
      + qt.second() *     1_000_000_000
      + qt.msec()   *         1_000_000
    )


# ── GStreamer pipeline ───────────────────────────────────────────────────────

class Pipeline(QObject):
    """
    Pipeline topology (two sinks in parallel via tee):

      souphttpsrc → hlsdemux → tsdemux ─┬─ h264parse → avdec_h264
                                         │
                                         └─ (audio, ignored for now)

      avdec_h264 → videoconvert → tee ─┬─ glsinkbin  (display, xid injected)
                                        └─ ndisink    (NDI output)

    When paused, GStreamer naturally holds the last decoded frame in the
    sink buffers, so NDI keeps sending it – fulfilling "freeze on last frame".

    For the NDI sink we use the gst-ndi element `ndisink`.  Adjust the
    `ndi-name` property to whatever you want receivers to see.
    """

    error_signal   = pyqtSignal(str)
    eos_signal     = pyqtSignal()
    state_changed  = pyqtSignal(str)   # "PLAYING" | "PAUSED" | "STOPPED"
    position_tick  = pyqtSignal(int)   # nanoseconds
    duration_ready = pyqtSignal(int)   # nanoseconds

    NDI_SOURCE_NAME = "HLS-NDI-Player"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pipe    = None
        self._bus_wid = None
        self._win_id  = None          # set before PLAYING
        self._duration_ns = 0
        self._freeze_frame_thread = None
        self._freeze_active = False

        # poll position every 500 ms
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._poll_position)
        self._pos_timer.setInterval(500)

    # ── public API ──────────────────────────────────────────────────────────

    def set_window_id(self, win_id: int):
        self._win_id = win_id

    def load(self, url: str):
        """Build (or rebuild) the pipeline for the given URL."""
        self._teardown()

        pipe_desc = (
            f'souphttpsrc location="{url}" ! '
            # HLS: hlsdemux handles m3u8 playlists; for plain TS/MP4 VOD it
            # falls through to tsdemux / qtdemux automatically.
            'decodebin name=dec '
            # We wire decodebin dynamically (see _on_pad_added)
        )

        # Build manually so we can wire the dynamic pad from decodebin
        self._pipe = Gst.Pipeline.new("hls-ndi-player")

        src = Gst.ElementFactory.make("souphttpsrc", "src")
        src.set_property("location", url)
        # Some HLS servers need a user-agent
        src.set_property("user-agent", "GStreamer HLS-NDI-Player/1.0")

        dec = Gst.ElementFactory.make("decodebin", "dec")

        self._pipe.add(src)
        self._pipe.add(dec)
        src.link(dec)

        # Video branch
        vconv  = Gst.ElementFactory.make("videoconvert",  "vconv")
        vscale = Gst.ElementFactory.make("videoscale",    "vscale")
        tee    = Gst.ElementFactory.make("tee",           "tee")

        # Display sink
        vsink  = Gst.ElementFactory.make("glsinkbin",     "vsink")
        if vsink is None:   # fallback
            vsink = Gst.ElementFactory.make("autovideosink", "vsink")

        # NDI sink  (requires gst-ndi plugin)
        ndisink = Gst.ElementFactory.make("ndisink", "ndisink")
        if ndisink is None:
            self.error_signal.emit(
                "gst-ndi plugin not found.\n"
                "Install it from https://github.com/teltek/gst-plugin-ndi "
                "and ensure the NDI SDK runtime is on your system."
            )
            return

        ndisink.set_property("ndi-name", self.NDI_SOURCE_NAME)

        # Queue elements to decouple tee branches
        q_display = Gst.ElementFactory.make("queue", "q_display")
        q_ndi     = Gst.ElementFactory.make("queue", "q_ndi")
        q_display.set_property("max-size-time",   200_000_000)  # 200 ms
        q_ndi    .set_property("max-size-time",   200_000_000)

        for el in (vconv, vscale, tee, q_display, vsink, q_ndi, ndisink):
            self._pipe.add(el)

        vconv.link(vscale)
        vscale.link(tee)

        tee_src_display = tee.get_request_pad("src_%u")
        q_display_sink  = q_display.get_static_pad("sink")
        tee_src_display.link(q_display_sink)
        q_display.link(vsink)

        tee_src_ndi   = tee.get_request_pad("src_%u")
        q_ndi_sink    = q_ndi.get_static_pad("sink")
        tee_src_ndi.link(q_ndi_sink)
        q_ndi.link(ndisink)

        # Store refs for the dynamic pad callback
        self._vconv = vconv

        dec.connect("pad-added", self._on_pad_added)

        # Bus
        bus = self._pipe.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        bus.connect("message", self._on_bus_message)
        bus.connect("sync-message::element", self._on_sync_message)

        self._pipe.set_state(Gst.State.PAUSED)

    def play(self):
        if self._pipe:
            self._freeze_active = False
            self._pipe.set_state(Gst.State.PLAYING)
            self._pos_timer.start()
            self.state_changed.emit("PLAYING")

    def pause(self):
        if self._pipe:
            self._pipe.set_state(Gst.State.PAUSED)
            self._pos_timer.stop()
            self.state_changed.emit("PAUSED")
            # NDI sink keeps sending last buffer while in PAUSED – no extra
            # work needed when using ndisink from gst-ndi.

    def seek(self, position_ns: int):
        """Seek to position in nanoseconds."""
        if self._pipe:
            self._pipe.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                position_ns,
            )

    def set_start_timecode(self, position_ns: int):
        """Seek to position (used when setting start timecode while paused)."""
        self.seek(position_ns)

    def get_position(self) -> int:
        if not self._pipe:
            return 0
        ok, pos = self._pipe.query_position(Gst.Format.TIME)
        return pos if ok else 0

    def get_duration(self) -> int:
        if not self._pipe:
            return 0
        ok, dur = self._pipe.query_duration(Gst.Format.TIME)
        return dur if ok else 0

    def stop(self):
        self._teardown()
        self.state_changed.emit("STOPPED")

    # ── internal ────────────────────────────────────────────────────────────

    def _on_pad_added(self, dec, pad):
        """Wire decodebin's dynamic video pad into our video branch."""
        caps = pad.get_current_caps()
        if not caps:
            caps = pad.query_caps(None)
        struct = caps.get_structure(0)
        name   = struct.get_name()

        if name.startswith("video/"):
            sink_pad = self._vconv.get_static_pad("sink")
            if not sink_pad.is_linked():
                pad.link(sink_pad)
                # Set window handle once video is flowing
                if self._win_id:
                    self._pipe.get_by_name("vsink").set_window_handle(self._win_id)

    def _on_bus_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self.error_signal.emit(f"{err.message}\n\nDebug: {dbg}")
            self._teardown()
        elif t == Gst.MessageType.EOS:
            self.eos_signal.emit()
        elif t == Gst.MessageType.DURATION_CHANGED:
            dur = self.get_duration()
            if dur > 0:
                self._duration_ns = dur
                self.duration_ready.emit(dur)
        elif t == Gst.MessageType.ASYNC_DONE:
            # First time we have a valid duration
            dur = self.get_duration()
            if dur > 0 and self._duration_ns == 0:
                self._duration_ns = dur
                self.duration_ready.emit(dur)

    def _on_sync_message(self, bus, msg):
        """Inject the native window handle for the display sink."""
        if msg.get_structure().get_name() == "prepare-window-handle":
            msg.src.set_window_handle(self._win_id)

    def _poll_position(self):
        pos = self.get_position()
        self.position_tick.emit(pos)

    def _teardown(self):
        self._pos_timer.stop()
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
            self._pipe = None


# ── Main window ─────────────────────────────────────────────────────────────

DARK_BG     = "#0d0f14"
PANEL_BG    = "#13161e"
ACCENT      = "#00d4aa"
ACCENT_DIM  = "#00a882"
TEXT        = "#e8eaf0"
TEXT_DIM    = "#6b7280"
BORDER      = "#1e2330"
DANGER      = "#ff4d6d"

STYLE = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG};
    color: {TEXT};
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
}}

QLabel {{
    color: {TEXT};
}}

QLabel#dim {{
    color: {TEXT_DIM};
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
}}

QLineEdit {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT};
    padding: 8px 12px;
    font-size: 13px;
    selection-background-color: {ACCENT};
}}

QLineEdit:focus {{
    border-color: {ACCENT};
}}

QPushButton {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT};
    padding: 8px 18px;
    font-size: 12px;
    letter-spacing: 0.5px;
}}

QPushButton:hover {{
    background: #1a1f2e;
    border-color: {ACCENT};
    color: {ACCENT};
}}

QPushButton:pressed {{
    background: {ACCENT};
    color: {TEXT_DIM};
}}

QPushButton#primary {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {TEXT_DIM};
    font-weight: bold;
}}

QPushButton#primary:hover {{
    background: {ACCENT_DIM};
    border-color: {ACCENT_DIM};
    color: {TEXT_DIM};
}}

QPushButton#danger {{
    border-color: {DANGER};
    color: {DANGER};
}}

QPushButton#danger:hover {{
    background: {DANGER};
    color: white;
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: {ACCENT};
    border: 2px solid {DARK_BG};
}}

QTimeEdit {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {ACCENT};
    padding: 6px 10px;
    font-size: 13px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
}}

QTimeEdit:focus {{
    border-color: {ACCENT};
}}

QFrame#separator {{
    color: {BORDER};
}}

QStatusBar {{
    background: {PANEL_BG};
    color: {TEXT_DIM};
    font-size: 11px;
    border-top: 1px solid {BORDER};
}}

QStatusBar::item {{
    border: none;
}}
"""


class VideoWidget(QWidget):
    """Native widget whose winId() is passed to GStreamer."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(854, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(f"background: #000;")
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HLS → NDI Player")
        self.setMinimumSize(960, 680)

        self._pipeline   = Pipeline()
        self._duration_ns = 0
        self._is_live     = False   # True for live HLS streams (no duration)
        self._scrubbing   = False

        self._connect_pipeline()
        self._build_ui()
        self.setStyleSheet(STYLE)

    # ── Pipeline wiring ─────────────────────────────────────────────────────

    def _connect_pipeline(self):
        p = self._pipeline
        p.error_signal  .connect(self._on_error)
        p.eos_signal    .connect(self._on_eos)
        p.state_changed .connect(self._on_state_changed)
        p.position_tick .connect(self._on_position_tick)
        p.duration_ready.connect(self._on_duration_ready)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── URL bar ──
        url_bar = QWidget()
        url_bar.setStyleSheet(f"background:{PANEL_BG}; border-bottom:1px solid {BORDER};")
        url_layout = QHBoxLayout(url_bar)
        url_layout.setContentsMargins(16, 12, 16, 12)
        url_layout.setSpacing(10)

        logo = QLabel("◈ NDI")
        logo.setStyleSheet(f"color:{ACCENT}; font-size:15px; font-weight:bold; letter-spacing:2px;")

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("Enter HLS / VOD URL  (http://…/stream.m3u8)")
        self._url_input.returnPressed.connect(self._load_stream)

        load_btn = QPushButton("LOAD")
        load_btn.setObjectName("primary")
        load_btn.setFixedWidth(80)
        load_btn.clicked.connect(self._load_stream)

        url_layout.addWidget(logo)
        url_layout.addWidget(self._url_input, 1)
        url_layout.addWidget(load_btn)
        layout.addWidget(url_bar)

        # ── Video area ──
        self._video = VideoWidget()
        layout.addWidget(self._video, 1)

        # ── NDI badge overlay hint ──
        ndi_badge = QLabel("● NDI  " + Pipeline.NDI_SOURCE_NAME)
        ndi_badge.setAlignment(Qt.AlignmentFlag.AlignRight)
        ndi_badge.setStyleSheet(
            f"color:{ACCENT}; background:rgba(0,0,0,120); "
            f"font-size:10px; letter-spacing:1px; padding:3px 8px;"
        )

        # ── Controls panel ──
        ctrl_panel = QWidget()
        ctrl_panel.setStyleSheet(f"background:{PANEL_BG}; border-top:1px solid {BORDER};")
        ctrl_layout = QVBoxLayout(ctrl_panel)
        ctrl_layout.setContentsMargins(16, 12, 16, 14)
        ctrl_layout.setSpacing(10)

        # Seek slider
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.setValue(0)
        self._slider.setEnabled(False)
        self._slider.sliderPressed .connect(self._slider_pressed)
        self._slider.sliderReleased.connect(self._slider_released)
        ctrl_layout.addWidget(self._slider)

        # Time labels row
        time_row = QHBoxLayout()
        time_row.setSpacing(0)
        self._pos_label = QLabel("00:00:00")
        self._pos_label.setStyleSheet(f"color:{ACCENT}; font-size:12px; letter-spacing:1px;")
        self._dur_label = QLabel("--:--:--")
        self._dur_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:12px;")
        time_row.addWidget(self._pos_label)
        time_row.addStretch()
        time_row.addWidget(self._dur_label)
        ctrl_layout.addLayout(time_row)

        # Buttons + timecode row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._play_btn = QPushButton("▶  PLAY")
        self._play_btn.setObjectName("primary")
        self._play_btn.setEnabled(False)
        self._play_btn.clicked.connect(self._toggle_play)

        stop_btn = QPushButton("■  STOP")
        stop_btn.setObjectName("danger")
        stop_btn.clicked.connect(self._stop)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setObjectName("separator")
        sep.setFixedWidth(1)

        tc_label = QLabel("START TC")
        tc_label.setObjectName("dim")
        tc_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._tc_edit = QTimeEdit()
        self._tc_edit.setDisplayFormat("HH:mm:ss.zzz")
        self._tc_edit.setEnabled(False)
        self._tc_edit.setFixedWidth(140)

        set_tc_btn = QPushButton("SET")
        set_tc_btn.setFixedWidth(54)
        set_tc_btn.clicked.connect(self._set_timecode)

        ndi_pill = QLabel("◈ NDI LIVE")
        ndi_pill.setStyleSheet(
            f"color:{DARK_BG}; background:{ACCENT}; "
            f"border-radius:10px; padding:3px 10px; "
            f"font-size:10px; font-weight:bold; letter-spacing:1px;"
        )
        ndi_pill.setVisible(False)
        self._ndi_pill = ndi_pill

        btn_row.addWidget(self._play_btn)
        btn_row.addWidget(stop_btn)
        btn_row.addWidget(sep)
        btn_row.addStretch()
        btn_row.addWidget(tc_label)
        btn_row.addWidget(self._tc_edit)
        btn_row.addWidget(set_tc_btn)
        btn_row.addSpacing(16)
        btn_row.addWidget(ndi_pill)

        ctrl_layout.addLayout(btn_row)
        layout.addWidget(ctrl_panel)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready  ·  Load an HLS or VOD URL to begin")

    # ── Slots ────────────────────────────────────────────────────────────────

    def _load_stream(self):
        url = self._url_input.text().strip()
        if not url:
            return
        self._status.showMessage(f"Loading  {url} …")
        self._pipeline.set_window_id(int(self._video.winId()))
        self._pipeline.load(url)
        self._pipeline.play()
        self._play_btn.setEnabled(True)
        self._tc_edit.setEnabled(True)
        self._ndi_pill.setVisible(True)

    def _toggle_play(self):
        if not self._pipeline._pipe:
            return
        ok, state, _ = self._pipeline._pipe.get_state(0)
        if state == Gst.State.PLAYING:
            self._pipeline.pause()
        else:
            self._pipeline.play()

    def _stop(self):
        self._pipeline.stop()
        self._slider.setValue(0)
        self._slider.setEnabled(False)
        self._pos_label.setText("00:00:00")
        self._dur_label.setText("--:--:--")
        self._play_btn.setText("▶  PLAY")
        self._play_btn.setEnabled(False)
        self._tc_edit.setEnabled(False)
        self._ndi_pill.setVisible(False)
        self._status.showMessage("Stopped")

    def _set_timecode(self):
        if not self._pipeline._pipe:
            return
        tc_ns = qtime_to_ns(self._tc_edit.time())
        self._pipeline.set_start_timecode(tc_ns)
        self._status.showMessage(
            f"Seeked to {self._tc_edit.time().toString('HH:mm:ss.zzz')}"
        )

    def _slider_pressed(self):
        self._scrubbing = True

    def _slider_released(self):
        if self._duration_ns > 0:
            frac = self._slider.value() / 1000.0
            seek_ns = int(frac * self._duration_ns)
            self._pipeline.seek(seek_ns)
        self._scrubbing = False

    # ── Pipeline callbacks ───────────────────────────────────────────────────

    def _on_error(self, msg: str):
        QMessageBox.critical(self, "GStreamer Error", msg)
        self._status.showMessage("Error — see dialog")

    def _on_eos(self):
        self._status.showMessage("End of stream")
        self._play_btn.setText("▶  PLAY")

    def _on_state_changed(self, state: str):
        if state == "PLAYING":
            self._play_btn.setText("⏸  PAUSE")
            self._status.showMessage("Playing  ·  NDI output active")
        elif state == "PAUSED":
            self._play_btn.setText("▶  PLAY")
            self._status.showMessage("Paused  ·  NDI frozen on last frame")
        elif state == "STOPPED":
            self._play_btn.setText("▶  PLAY")

    def _on_position_tick(self, pos_ns: int):
        qt = ns_to_qtime(pos_ns)
        self._pos_label.setText(qt.toString("HH:mm:ss"))
        if self._duration_ns > 0 and not self._scrubbing:
            frac = pos_ns / self._duration_ns
            self._slider.setValue(int(frac * 1000))

    def _on_duration_ready(self, dur_ns: int):
        self._duration_ns = dur_ns
        self._slider.setEnabled(True)
        self._dur_label.setText(ns_to_qtime(dur_ns).toString("HH:mm:ss"))
        self._is_live = (dur_ns == 0)

    def closeEvent(self, event):
        self._pipeline.stop()
        super().closeEvent(event)


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    # GLib main loop (needed for GStreamer bus on some platforms)
    glib_loop = GLib.MainLoop()
    glib_thread = threading.Thread(target=glib_loop.run, daemon=True)
    glib_thread.start()

    app = QApplication(sys.argv)
    app.setApplicationName("HLS NDI Player")

    win = MainWindow()
    win.show()

    ret = app.exec()
    glib_loop.quit()
    sys.exit(ret)


if __name__ == "__main__":
    main()
