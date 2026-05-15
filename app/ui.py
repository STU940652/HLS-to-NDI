"""GTK 4 user interface: transport controls, timeline, preview, NDI name."""

from __future__ import annotations

import logging
import sys
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gtk, Gst  # noqa: E402

from app.gst_utils import (
    NDI_SDK_DOWNLOAD_URL,
    format_ns,
    ndi_sdk_runtime_probe_error,
    parse_time_string,
    plugin_available,
)
from app.ndi_output import NdiOutputPipeline
from app.player import PlaybackPipeline

logger = logging.getLogger(__name__)

# GStreamer must be initialized before player
import app.gst_utils  # noqa: F401, E402


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title="GTK + GStreamer NDI Player")
        self.set_default_size(1024, 700)

        self._ndi = NdiOutputPipeline(ndi_name="GTK_NDI_Player", on_error=self._on_ndi_error)
        self._player = PlaybackPipeline(
            on_error=self._on_playback_error,
            on_eos=self._on_eos,
            on_state_changed=self._on_state_changed,
            on_duration_changed=self._on_duration_changed,
            on_video_branch_ready=self._on_video_branch_ready,
        )

        self._position_timer: int = 0
        self._user_scrubbing = False  # True while user adjusts scale (debounced); blocks timer overwrite
        self._seek_debounce_id: int = 0
        self._scale_suppress = False
        self._gtksink_widget: Optional[Gtk.Widget] = None

        self._build_ui()

        # NDI runs for the whole app session (separate pipeline, always on).
        sdk_err = ndi_sdk_runtime_probe_error()
        if sdk_err is not None:
            self._present_ndi_sdk_install_dialog(sdk_err)
        else:
            try:
                self._ndi.start()
            except Exception as exc:
                self._set_status(f"NDI: {exc}")

        self._start_position_timer()
        self.connect("close-request", self._on_close_request)

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        self.set_child(outer)

        # Top bar: URL + NDI name
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row1.append(Gtk.Label(label="Stream URL:"))
        self._url = Gtk.Entry()
        self._url.set_hexpand(True)
        self._url.set_text("http://192.168.1.141:8888/demo/master.m3u8")
        self._url.set_placeholder_text("https://…/playlist.m3u8 or file:///…")
        row1.append(self._url)
        top.append(row1)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row2.append(Gtk.Label(label="NDI name:"))
        self._ndi_name = Gtk.Entry()
        self._ndi_name.set_text("GTK_NDI_Player")
        self._ndi_name.set_hexpand(True)
        apply_ndi = Gtk.Button(label="Apply NDI name")
        apply_ndi.connect("clicked", self._on_apply_ndi_name)
        row2.append(self._ndi_name)
        ndi_trademark = Gtk.Label()
        ndi_trademark.set_markup(
            '<a href="https://ndi.video/" title="NDI">'
            "NDI® is a registered trademark of Vizrt NDI AB</a>"
        )
        ndi_trademark.set_wrap(True)
        ndi_trademark.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        row2.append(ndi_trademark)
        row2.append(apply_ndi)
        top.append(row2)
        outer.append(top)

        # Transport
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._btn_play = Gtk.Button(label="Play")
        self._btn_play.connect("clicked", self._on_play)
        self._btn_pause = Gtk.Button(label="Pause")
        self._btn_pause.connect("clicked", self._on_pause)
        self._btn_stop = Gtk.Button(label="Stop")
        self._btn_stop.connect("clicked", self._on_stop)
        for b in (self._btn_play, self._btn_pause, self._btn_stop):
            btn_row.append(b)
        outer.append(btn_row)

        # Preview: Gtk.Picture + gtk4paintablesink (Gtk.Video.set_paintable needs GTK 4.14+)
        self._preview_picture = Gtk.Picture()
        self._preview_picture.set_vexpand(True)
        self._preview_picture.set_hexpand(True)
        if hasattr(self._preview_picture, "set_can_shrink"):
            self._preview_picture.set_can_shrink(True)
        if hasattr(Gtk, "ContentFit") and hasattr(self._preview_picture, "set_content_fit"):
            self._preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._sink_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._sink_host.set_vexpand(True)
        self._sink_host.set_hexpand(True)
        preview_frame = Gtk.Frame()
        preview_frame.set_child(self._preview_picture)
        preview_frame.set_vexpand(True)
        outer.append(preview_frame)
        # gtksink alternative (hidden until used)
        self._sink_host.set_visible(False)
        outer.append(self._sink_host)

        # Timeline
        time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._pos_label = Gtk.Label(label=format_ns(0))
        self._pos_label.set_width_chars(16)
        self._dur_label = Gtk.Label(label=format_ns(0))
        self._dur_label.set_width_chars(16)
        self._scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self._scale.set_range(0.0, 1.0)
        self._scale.set_draw_value(True)
        self._scale.set_digits(3)
        self._scale.set_hexpand(True)
        self._scale.set_sensitive(False)
        self._scale.connect("value-changed", self._on_scale_value_changed)
        time_row.append(self._pos_label)
        time_row.append(self._scale)
        time_row.append(self._dur_label)
        outer.append(time_row)

        # Jump to time
        jump = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        jump.append(Gtk.Label(label="Go to time:"))
        self._time_entry = Gtk.Entry()
        self._time_entry.set_placeholder_text("H:MM:SS.mmm or MM:SS.mmm")
        self._time_entry.set_hexpand(True)
        go = Gtk.Button(label="Seek")
        go.connect("clicked", self._on_seek_entry)
        jump.append(self._time_entry)
        jump.append(go)
        outer.append(jump)

        self._status = Gtk.Label(label="")
        self._status.set_xalign(0.0)
        self._status.set_wrap(True)
        outer.append(self._status)

    def _set_status(self, text: str) -> None:
        self._status.set_text(text)
        if text:
            logger.info("Status: %s", text)

    def _present_ndi_sdk_install_dialog(self, tech_reason: str) -> None:
        """Blocking modal: NDI runtime/SDK missing while GStreamer ndisink is present."""
        body = (
            "The NDI SDK runtime does not appear to be installed or cannot be loaded. "
            "NDI output is disabled until it is installed.\n\n"
            f"Technical detail:\n{tech_reason}\n\n"
            "Download and install the NDI SDK from:\n"
            f"{NDI_SDK_DOWNLOAD_URL}\n\n"
            "Restart this application after installing."
        )
        loop = GLib.MainLoop()

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="NDI SDK required",
        )
        # PyGObject’s Gtk override adds format_secondary_text; raw introspection may not.
        dialog.set_property("secondary-use-markup", False)
        dialog.set_property("secondary-text", body)

        def on_response(dlg: Gtk.MessageDialog, _response_id: int) -> None:
            dlg.destroy()
            loop.quit()

        dialog.connect("response", on_response)
        dialog.present()
        loop.run()
        self._set_status(
            "NDI: disabled — install the NDI SDK (see dialog). Preview and playback still work."
        )

    def _on_playback_error(self, msg: str) -> None:
        GLib.idle_add(self._set_status, f"Playback: {msg}")

    def _on_ndi_error(self, msg: str) -> None:
        GLib.idle_add(self._set_status, msg)

    def _on_eos(self) -> None:
        def _ui() -> None:
            self._set_status("End of stream")
            self._btn_play.set_sensitive(True)

        GLib.idle_add(_ui)

    def _on_state_changed(self, state) -> None:  # type: ignore[no-untyped-def]
        def _ui() -> None:
            self._refresh_duration()

        GLib.idle_add(_ui)

    def _on_duration_changed(self, _duration_ns: int) -> None:
        def _ui() -> None:
            self._sync_timeline_from_player()

        GLib.idle_add(_ui)

    def _on_video_branch_ready(self) -> None:
        def _ui() -> bool:
            self._attach_preview()
            return False

        GLib.idle_add(_ui)

    def _on_apply_ndi_name(self, _btn: Gtk.Button) -> None:
        name = self._ndi_name.get_text().strip() or "GTK_NDI_Player"
        self._ndi.set_ndi_name(name)
        self._set_status(f"NDI name set to “{name}” (may require NDI pipeline restart to take effect on some plugins).")

    @staticmethod
    def _normalize_uri(text: str) -> str:
        t = text.strip()
        if not t:
            return t
        if "://" not in t:
            if t.startswith("/"):
                return "file://" + t
            return "https://" + t
        return t

    def _on_play(self, _btn: Optional[Gtk.Button] = None) -> None:
        uri = self._normalize_uri(self._url.get_text())
        if not uri:
            self._set_status("Enter a stream URL")
            return
        if not any(
            (
                plugin_available("gtk4paintablesink"),
                plugin_available("gtksink"),
                plugin_available("glimagesink"),
            )
        ):
            self._set_status("No video sink (gtk4paintablesink / gtksink / glimagesink) found.")
            return
        try:
            if self._player.pipeline and self._player.uri == uri:
                self._player.play()
            else:
                self._rebuild_preview_container()
                self._player.build(uri)
                self._player.play()
            self._set_status("Playing")
            self._btn_play.set_sensitive(True)
        except Exception as exc:
            self._set_status(str(exc))
            logger.exception("Play failed")

    def _rebuild_preview_container(self) -> None:
        if self._gtksink_widget is not None:
            self._sink_host.remove(self._gtksink_widget)
            self._gtksink_widget = None
        self._preview_picture.set_visible(True)
        self._sink_host.set_visible(False)

    def _attach_preview(self) -> bool:
        paintable = self._player.get_preview_paintable()
        if paintable is not None:
            self._preview_picture.set_paintable(paintable)
            self._preview_picture.set_visible(True)
            self._sink_host.set_visible(False)
            return True

        w = self._player.get_preview_widget()
        if w is not None:
            self._gtksink_widget = w
            self._sink_host.append(w)
            self._preview_picture.set_visible(False)
            self._sink_host.set_visible(True)
            return True
        return False

    def _on_pause(self, _btn: Gtk.Button) -> None:
        self._player.pause()
        self._set_status("Paused")

    def _on_stop(self, _btn: Gtk.Button) -> None:
        self._player.stop()
        self._rebuild_preview_container()
        self._preview_picture.set_paintable(None)
        self._dur_label.set_text(format_ns(0))
        self._scale_suppress = True
        self._scale.set_value(0.0)
        self._scale_suppress = False
        self._set_status("Stopped")
        self._update_position_display()

    def _on_scale_value_changed(self, scale: Gtk.Scale) -> None:
        """Gtk.Scale often never delivers GestureClick 'released' after dragging the thumb, so we debounce seek here."""
        if self._scale_suppress:
            return
        okp, pos = self._player.query_position()
        pos_ns = int(pos) if okp and pos != Gst.CLOCK_TIME_NONE else 0
        ok, a, b, _lo = self._player.get_timeline_for_ui(pos_ns)
        if not ok or b <= a:
            return
        self._user_scrubbing = True
        frac = float(scale.get_value())
        preview_ns = int(a + frac * (b - a))
        self._pos_label.set_text(format_ns(preview_ns))

        if self._seek_debounce_id:
            GLib.source_remove(self._seek_debounce_id)
        self._seek_debounce_id = GLib.timeout_add(120, self._finish_scale_seek)

    def _finish_scale_seek(self) -> bool:
        self._seek_debounce_id = 0
        self._user_scrubbing = False
        self._seek_to_scale()
        return False  # GLib: single shot

    def _seek_to_scale(self) -> None:
        okp, pos = self._player.query_position()
        pos_ns = int(pos) if okp and pos != Gst.CLOCK_TIME_NONE else 0
        ok, a, b, _lo = self._player.get_timeline_for_ui(pos_ns)
        if not ok or b <= a:
            return
        frac = float(self._scale.get_value())
        target = int(a + frac * (b - a))
        if self._player.seek_simple(target):
            self._set_status(f"Seek: {format_ns(target)}")
        else:
            self._set_status(f"Seek failed at {format_ns(target)}")

    def _on_seek_entry(self, _btn: Gtk.Button) -> None:
        text = self._time_entry.get_text()
        ns = parse_time_string(text)
        if ns is None:
            self._set_status("Invalid time format. Use e.g. 1:23:45.500 or 90.5")
            return
        okp, pos = self._player.query_position()
        pos_ns = int(pos) if okp and pos != Gst.CLOCK_TIME_NONE else 0
        if not self._player.scrubbing_allowed(pos_ns):
            self._set_status("Timeline span not available yet, or stream does not allow seeking.")
            return
        if self._player.seek_accurate(ns):
            self._set_status(f"Seek: {format_ns(ns)}")
        else:
            self._set_status("Seek failed")

    def _start_position_timer(self) -> None:
        def tick() -> bool:
            self._update_position_display()
            return True  # continue

        self._position_timer = GLib.timeout_add(250, tick)

    def _playback_position_ns(self) -> int:
        ok, pos = self._player.query_position()
        if ok and pos != Gst.CLOCK_TIME_NONE:
            return int(pos)
        return 0

    def _sync_timeline_from_player(self) -> None:
        pos_ns = self._playback_position_ns()
        ok, a, b, _lo = self._player.get_timeline_for_ui(pos_ns)
        if ok:
            self._dur_label.set_text(format_ns(b))
        else:
            ok_dur, dur = self._player.query_duration()
            if ok_dur and dur != Gst.CLOCK_TIME_NONE:
                self._dur_label.set_text(format_ns(int(dur)))
            else:
                self._dur_label.set_text("--:--:--.---")
        self._update_seek_ui()

    def _update_position_display(self) -> None:
        if self._user_scrubbing:
            return
        pos_ns = self._playback_position_ns()
        ok, a, b, _lo = self._player.get_timeline_for_ui(pos_ns)
        self._pos_label.set_text(format_ns(pos_ns))
        if ok:
            self._dur_label.set_text(format_ns(b))
        if self._player.scrubbing_allowed(pos_ns):
            frac = (float(pos_ns) - float(a)) / float(b - a)
            frac = max(0.0, min(1.0, frac))
            self._scale_suppress = True
            self._scale.set_value(frac)
            self._scale_suppress = False

    def _refresh_duration(self) -> None:
        self._sync_timeline_from_player()

    def _update_seek_ui(self) -> None:
        pos_ns = self._playback_position_ns()
        can = self._player.scrubbing_allowed(pos_ns)
        self._scale.set_sensitive(can)
        if not can:
            self._scale_suppress = True
            self._scale.set_value(0.0)
            self._scale_suppress = False

    def _on_close_request(self, _win) -> bool:  # type: ignore[no-untyped-def]
        if self._seek_debounce_id:
            GLib.source_remove(self._seek_debounce_id)
            self._seek_debounce_id = 0
        if self._position_timer:
            GLib.source_remove(self._position_timer)
            self._position_timer = 0
        self._player.teardown()
        self._ndi.stop()
        return False


class App(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="com.example.gtk_ndi_player")

    def do_activate(self) -> None:  # type: ignore[override]
        win = MainWindow(self)
        win.present()


def run() -> int:
    logging.basicConfig(level=logging.INFO)
    app = App()
    return app.run(sys.argv)
