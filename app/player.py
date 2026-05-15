"""Main playback pipeline: URI decode, preview, inter-sinks for NDI bridge."""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from app.gst_utils import (
    INTER_AUDIO_CHANNEL,
    INTER_AUDIO_CAPS_STR,
    INTER_VIDEO_CHANNEL,
    INTER_VIDEO_CAPS_STR,
    decode_bin_available,
    decode_bin_element,
    missing_plugins,
    plugin_available,
)
from app.gst_utils import REQUIRED_PLAYBACK_PLUGINS

logger = logging.getLogger(__name__)

# Set to e.g. "*:2,adaptivedemux*:6,hls*:6,uridecodebin*:5" for console Gst debug (same idea as GST_DEBUG).
_ENV_GST_DEBUG = "PLAYBACK_PIPELINE_GST_DEBUG"
# If d3d12h264dec rejects caps on your system, set to 1 so decodebin prefers another H.264 decoder.
_ENV_DEMOTE_D3D_H264 = "PLAYBACK_PIPELINE_DEMOTE_D3D_H264"

OPTIONAL_PREVIEW = ("gtk4paintablesink", "gtksink", "glimagesink")


def _caps_for_log(caps: Optional[Gst.Caps]) -> str:
    if caps is None:
        return "(null caps)"
    if caps.is_empty():
        return "(empty caps)"
    return caps.to_string()


def _element_path(obj: Gst.Object) -> str:
    try:
        return obj.get_path_string()
    except Exception:
        try:
            return obj.get_name()
        except Exception:
            return repr(obj)


def _configure_playback_gst_debug() -> None:
    spec = os.environ.get(_ENV_GST_DEBUG, "").strip()
    if not spec:
        return
    Gst.debug_set_active(True)
    try:
        Gst.debug_set_threshold_from_string(spec, True)
    except TypeError:
        Gst.debug_set_threshold_from_string(spec)
    logger.info("GStreamer debug enabled via %s=%r", _ENV_GST_DEBUG, spec)


def _maybe_demote_d3d_h264_decoders() -> None:
    v = os.environ.get(_ENV_DEMOTE_D3D_H264, "").strip().lower()
    if v not in ("1", "true", "yes", "on"):
        return
    reg = Gst.Registry.get()
    for name in ("d3d12h264dec", "d3d11h264dec"):
        feat = reg.lookup_feature(name)
        if feat is None:
            continue
        try:
            old = feat.get_rank()
            feat.set_rank(Gst.Rank.NONE)
            logger.info(
                "Demoted %s decoder rank (%s -> NONE). Was rank=%s",
                name,
                _ENV_DEMOTE_D3D_H264,
                old,
            )
        except Exception:
            logger.exception("Could not demote decoder feature %s", name)


class PlaybackPipeline:
    def __init__(
        self,
        *,
        on_error: Optional[Callable[[str], None]] = None,
        on_eos: Optional[Callable[[], None]] = None,
        on_state_changed: Optional[Callable[[Gst.State], None]] = None,
        on_duration_changed: Optional[Callable[[int], None]] = None,
        on_video_branch_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_error = on_error
        self._on_eos = on_eos
        self._on_state_changed = on_state_changed
        self._on_duration_changed = on_duration_changed
        self._on_video_branch_ready = on_video_branch_ready

        self._pipeline: Optional[Gst.Pipeline] = None
        self._decode: Optional[Gst.Element] = None
        self._bus_watch_id: Optional[int] = None

        self._video_linked = False
        self._audio_linked = False

        self._uri: str = ""
        # Furthest stream time seen; grows while playing live/DVR so the scrub range extends.
        self._live_edge_ns: int = 0

    @property
    def pipeline(self) -> Optional[Gst.Pipeline]:
        return self._pipeline

    @property
    def uri(self) -> str:
        return self._uri

    def check_plugins(self) -> list[str]:
        miss = missing_plugins(REQUIRED_PLAYBACK_PLUGINS)
        if not decode_bin_available():
            miss.append("uridecodebin3")
        return miss

    @staticmethod
    def _link_elements_or_raise(
        links: list[tuple[Gst.Element, Gst.Element, str]],
        overall_msg: str,
    ) -> None:
        for src, sink, step in links:
            if not src.link(sink):
                raise RuntimeError(f"{overall_msg} ({step})")

    def build(self, uri: str) -> None:
        miss = self.check_plugins()
        if miss:
            raise RuntimeError("Missing GStreamer plugins: " + ", ".join(miss))

        self.teardown()

        _configure_playback_gst_debug()
        _maybe_demote_d3d_h264_decoders()

        self._uri = uri
        self._live_edge_ns = 0
        pipe = Gst.Pipeline.new("playback_pipeline")
        decode = decode_bin_element("decode")
        decode.set_property("uri", uri)
        pipe.add(decode)
        decode.connect("pad-added", self._on_pad_added)
        decode.connect("child-added", self._on_decode_child_added)

        bus = pipe.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)

        self._pipeline = pipe
        self._decode = decode
        self._video_linked = False
        self._audio_linked = False

    def _make_preview_sink(self) -> Gst.Element:
        for name in OPTIONAL_PREVIEW:
            if plugin_available(name):
                sink = Gst.ElementFactory.make(name, "preview_sink")
                if sink:
                    return sink
        raise RuntimeError(
            "No suitable video sink found. Install GTK GLib bindings with gst-plugins-good "
            "(gtk4paintablesink/gtksink) or use glimagesink."
        )

    @staticmethod
    def _link_tee_branch(tee_pad: Gst.Pad, queue_el: Gst.Element, sink: Gst.Element) -> bool:
        sink_pad = queue_el.get_static_pad("sink")
        ret = tee_pad.link(sink_pad)
        if ret != Gst.PadLinkReturn.OK:
            logger.error("tee -> queue link failed: %s", ret.value_nick)
            return False
        return queue_el.link(sink)

    @staticmethod
    def _configure_queue(q: Gst.Element) -> None:
        q.set_property("max-size-buffers", 0)
        q.set_property("max-size-time", 2 * Gst.SECOND)
        q.set_property("max-size-bytes", 0)

    def _create_video_branch(self) -> Gst.Element:
        if not self._pipeline:
            raise RuntimeError("Playback pipeline is not available.")

        qv = Gst.ElementFactory.make("queue", "qv")
        vconvert = Gst.ElementFactory.make("videoconvert", "vconvert")
        tee_v = Gst.ElementFactory.make("tee", "tee_v")
        q_prev_v = Gst.ElementFactory.make("queue", "q_preview_v")
        q_ndi_v = Gst.ElementFactory.make("queue", "q_ndi_v")
        vrate_ndi = Gst.ElementFactory.make("videorate", "vrate_ndi")
        vconvert_ndi = Gst.ElementFactory.make("videoconvert", "vconvert_ndi")
        vscale_ndi = Gst.ElementFactory.make("videoscale", "vscale_ndi")
        preview_sink = self._make_preview_sink()
        inter_v_caps = Gst.ElementFactory.make("capsfilter", "inter_video_sink_caps")
        inter_v = Gst.ElementFactory.make("intervideosink", "inter_video_sink")

        if not all(
            [
                qv,
                vconvert,
                tee_v,
                q_prev_v,
                q_ndi_v,
                vrate_ndi,
                vconvert_ndi,
                vscale_ndi,
                preview_sink,
                inter_v_caps,
                inter_v,
            ]
        ):
            raise RuntimeError("Failed to create video pipeline elements.")

        inter_v_caps.set_property("caps", Gst.Caps.from_string(INTER_VIDEO_CAPS_STR))
        inter_v.set_property("channel", INTER_VIDEO_CHANNEL)
        for q in (qv, q_prev_v, q_ndi_v):
            self._configure_queue(q)

        for el in (
            qv,
            vconvert,
            tee_v,
            q_prev_v,
            q_ndi_v,
            vrate_ndi,
            vconvert_ndi,
            vscale_ndi,
            preview_sink,
            inter_v_caps,
            inter_v,
        ):
            self._pipeline.add(el)

        logger.info(
            "video branch inter caps target: %s",
            INTER_VIDEO_CAPS_STR,
        )
        self._link_elements_or_raise(
            [
                (qv, vconvert, "video queue -> videoconvert"),
                (vconvert, tee_v, "videoconvert -> tee (video)"),
            ],
            "Could not link video chain before tee.",
        )

        t_src = tee_v.get_request_pad("src_%u")
        if not t_src:
            raise RuntimeError("Could not get video tee src pad.")
        if not self._link_tee_branch(t_src, q_prev_v, preview_sink):
            raise RuntimeError("Could not link video preview branch.")

        t_src2 = tee_v.get_request_pad("src_%u")
        if not t_src2:
            raise RuntimeError("Could not get second video tee src pad.")
        q_ndi_v_sink = q_ndi_v.get_static_pad("sink")
        if not q_ndi_v_sink:
            raise RuntimeError("Could not get video inter queue sink pad.")
        if t_src2.link(q_ndi_v_sink) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Could not link video tee to inter queue.")
        self._link_elements_or_raise(
            [
                (q_ndi_v, vrate_ndi, "inter queue -> videorate (NDI)"),
                (vrate_ndi, vconvert_ndi, "videorate -> videoconvert (NDI)"),
                (vconvert_ndi, vscale_ndi, "videoconvert -> videoscale (NDI)"),
                (vscale_ndi, inter_v_caps, "videoscale -> capsfilter (inter video)"),
                (inter_v_caps, inter_v, "capsfilter -> intervideosink"),
            ],
            "Could not link video inter branch.",
        )

        for el in (
            qv,
            vconvert,
            tee_v,
            q_prev_v,
            q_ndi_v,
            vrate_ndi,
            vconvert_ndi,
            vscale_ndi,
            preview_sink,
            inter_v_caps,
            inter_v,
        ):
            el.sync_state_with_parent()
        return qv

    def _create_audio_branch(self) -> Gst.Element:
        if not self._pipeline:
            raise RuntimeError("Playback pipeline is not available.")

        qa = Gst.ElementFactory.make("queue", "qa")
        aconvert = Gst.ElementFactory.make("audioconvert", "aconvert")
        aresample = Gst.ElementFactory.make("audioresample", "aresample")
        tee_a = Gst.ElementFactory.make("tee", "tee_a")
        q_mon_a = Gst.ElementFactory.make("queue", "q_monitor_a")
        q_ndi_a = Gst.ElementFactory.make("queue", "q_ndi_a")
        monitor_sink = Gst.ElementFactory.make("autoaudiosink", "monitor_audio")
        inter_a_caps = Gst.ElementFactory.make("capsfilter", "inter_audio_sink_caps")
        inter_a = Gst.ElementFactory.make("interaudiosink", "inter_audio_sink")

        if not all([qa, aconvert, aresample, tee_a, q_mon_a, q_ndi_a, monitor_sink, inter_a_caps, inter_a]):
            raise RuntimeError("Failed to create audio pipeline elements.")

        inter_a_caps.set_property("caps", Gst.Caps.from_string(INTER_AUDIO_CAPS_STR))
        inter_a.set_property("channel", INTER_AUDIO_CHANNEL)
        for q in (qa, q_mon_a, q_ndi_a):
            self._configure_queue(q)

        logger.info(
            "audio branch inter caps target: %s",
            INTER_AUDIO_CAPS_STR,
        )
        for el in (qa, aconvert, aresample, tee_a, q_mon_a, q_ndi_a, monitor_sink, inter_a_caps, inter_a):
            self._pipeline.add(el)

        self._link_elements_or_raise(
            [
                (qa, aconvert, "audio queue -> audioconvert"),
                (aconvert, aresample, "audioconvert -> audioresample"),
                (aresample, tee_a, "audioresample -> tee (audio)"),
            ],
            "Could not link audio chain before tee.",
        )

        ta1 = tee_a.get_request_pad("src_%u")
        ta2 = tee_a.get_request_pad("src_%u")
        if not ta1 or not ta2:
            raise RuntimeError("Could not get audio tee src pads.")
        if not self._link_tee_branch(ta1, q_mon_a, monitor_sink):
            raise RuntimeError("Could not link audio monitor branch.")
        q_ndi_a_sink = q_ndi_a.get_static_pad("sink")
        if not q_ndi_a_sink:
            raise RuntimeError("Could not get audio inter queue sink pad.")
        if ta2.link(q_ndi_a_sink) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Could not link audio tee to inter queue.")
        if not q_ndi_a.link(inter_a_caps) or not inter_a_caps.link(inter_a):
            raise RuntimeError("Could not link audio inter branch.")

        for el in (qa, aconvert, aresample, tee_a, q_mon_a, q_ndi_a, monitor_sink, inter_a_caps, inter_a):
            el.sync_state_with_parent()
        return qa

    def get_preview_paintable(self):  # type: ignore[no-untyped-def]
        """Return Gdk.Paintable from gtk4paintablesink if available."""
        if not self._pipeline:
            return None
        sink = self._pipeline.get_by_name("preview_sink")
        if sink is None:
            return None
        factory = sink.get_factory().get_name()
        if factory == "gtk4paintablesink":
            try:
                return sink.get_property("paintable")
            except Exception:
                return None
        return None

    def get_preview_widget(self):  # type: ignore[no-untyped-def]
        """Return GtkWidget from gtksink if used."""
        if not self._pipeline:
            return None
        sink = self._pipeline.get_by_name("preview_sink")
        if sink is None:
            return None
        factory = sink.get_factory().get_name()
        if factory == "gtksink":
            try:
                return sink.get_property("widget")
            except Exception:
                return None
        return None

    def _on_decode_child_added(self, decode_bin: Gst.Element, child: Gst.Element, name: str) -> None:
        try:
            factory = child.get_factory()
            factory_name = factory.get_name() if factory else "?"
        except Exception:
            factory_name = "?"
        logger.info(
            "decodebin child-added: decode=%s name=%r factory=%s path=%s",
            _element_path(decode_bin),
            name,
            factory_name,
            _element_path(child),
        )

    def _on_decode_pad_event_probe(
        self,
        pad: Gst.Pad,
        info: Gst.PadProbeInfo,
        _user_data: object,
    ) -> Gst.PadProbeReturn:
        ev = info.get_event()
        if ev is None:
            return Gst.PadProbeReturn.OK
        if ev.type == Gst.EventType.CAPS:
            caps = ev.parse_caps()
            logger.info(
                "decode src pad %s CAPS event (parent=%s): %s",
                pad.get_name(),
                _element_path(pad.get_parent()),
                _caps_for_log(caps),
            )
        elif ev.type == Gst.EventType.RECONFIGURE:
            logger.info(
                "decode src pad %s RECONFIGURE (parent=%s)",
                pad.get_name(),
                _element_path(pad.get_parent()),
            )
        return Gst.PadProbeReturn.OK

    def _attach_decode_pad_debug_probe(self, pad: Gst.Pad) -> None:
        try:
            pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_decode_pad_event_probe, None)
        except Exception:
            logger.exception("Could not attach decode pad debug probe on %s", pad.get_name())

    def _on_pad_added(
        self,
        element: Gst.Element,
        pad: Gst.Pad,
    ) -> None:
        if element != self._decode or not self._pipeline:
            return

        caps = pad.get_current_caps()
        if caps is None:
            caps = pad.query_caps(None)
        tmpl = pad.get_pad_template()
        tmpl_name: Optional[str] = None
        if tmpl is not None:
            tmpl_name = getattr(tmpl, "name_template", None) or tmpl.get_name()

        dir_nick = pad.get_direction().value_nick
        if caps is None or caps.is_empty():
            logger.warning(
                "decode pad-added: pad=%s direction=%s template=%s current_caps=%s (no caps yet; skipping link)",
                pad.get_name(),
                dir_nick,
                tmpl_name,
                _caps_for_log(caps),
            )
            return
        struct = caps.get_structure(0)
        name = struct.get_name()
        logger.info(
            "decode pad-added: pad=%s direction=%s template=%s media=%s caps=%s",
            pad.get_name(),
            dir_nick,
            tmpl_name,
            name,
            _caps_for_log(caps),
        )

        if name.startswith("video/") and not self._video_linked:
            try:
                qv = self._create_video_branch()
                sinkpad = qv.get_static_pad("sink")
                if sinkpad and not pad.is_linked():
                    ret = pad.link(sinkpad)
                    logger.info(
                        "decode video pad link -> qv.sink: %s (pad=%s)",
                        ret.value_nick,
                        pad.get_name(),
                    )
                    if ret == Gst.PadLinkReturn.OK:
                        self._video_linked = True
                        self._attach_decode_pad_debug_probe(pad)
                        if self._on_video_branch_ready:
                            self._on_video_branch_ready()
                    else:
                        logger.error(
                            "decode video pad failed to link (downstream may stay not-negotiated): %s",
                            ret.value_nick,
                        )
            except Exception:
                logger.exception("Failed to add video branch after decode pad-added.")
        elif name.startswith("audio/") and not self._audio_linked:
            try:
                qa = self._create_audio_branch()
                sinkpad = qa.get_static_pad("sink")
                if sinkpad and not pad.is_linked():
                    ret = pad.link(sinkpad)
                    logger.info(
                        "decode audio pad link -> qa.sink: %s (pad=%s)",
                        ret.value_nick,
                        pad.get_name(),
                    )
                    if ret == Gst.PadLinkReturn.OK:
                        self._audio_linked = True
                        self._attach_decode_pad_debug_probe(pad)
                    else:
                        logger.error(
                            "decode audio pad failed to link (downstream may stay not-negotiated): %s",
                            ret.value_nick,
                        )
            except Exception:
                logger.exception("Failed to add audio branch after decode pad-added.")
        else:
            logger.info(
                "decode pad not handled as primary A/V (linked=%s audio=%s): %s",
                self._video_linked,
                self._audio_linked,
                name,
            )

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        mtype = message.type
        if mtype == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            src_path = _element_path(message.src)
            text = f"Playback error: {err.message} ({dbg})"
            logger.error(
                "Playback error from %s: %s (%s)",
                src_path,
                err.message,
                dbg,
            )
            if self._on_error:
                self._on_error(text)
        elif mtype == Gst.MessageType.EOS:
            if self._on_eos:
                self._on_eos()
        elif mtype == Gst.MessageType.STATE_CHANGED:
            if message.src == self._pipeline:
                old, new, pending = message.parse_state_changed()
                if pending == Gst.State.VOID_PENDING and self._on_state_changed:
                    self._on_state_changed(new)
        elif mtype == Gst.MessageType.DURATION_CHANGED:
            if self._on_duration_changed and self._pipeline:
                ok, dur = self._pipeline.query_duration(Gst.Format.TIME)
                if ok and dur != Gst.CLOCK_TIME_NONE:
                    self._on_duration_changed(dur)
        elif mtype == Gst.MessageType.WARNING:
            warn, dbg = message.parse_warning()
            logger.warning(
                "Playback warning from %s: %s (%s)",
                _element_path(message.src),
                warn.message,
                dbg,
            )

    def play(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.PLAYING)

    def pause(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.PAUSED)

    def stop(self) -> None:
        self.teardown()

    def teardown(self) -> None:
        if self._pipeline:
            bus = self._pipeline.get_bus()
            if self._bus_watch_id is not None:
                bus.disconnect(self._bus_watch_id)
                self._bus_watch_id = None
            bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._decode = None
        self._video_linked = False
        self._audio_linked = False
        self._uri = ""
        self._live_edge_ns = 0

    def query_position(self) -> tuple[bool, int]:
        if not self._pipeline:
            return False, 0
        return self._pipeline.query_position(Gst.Format.TIME)

    def query_duration(self) -> tuple[bool, int]:
        if not self._pipeline:
            return False, Gst.CLOCK_TIME_NONE
        return self._pipeline.query_duration(Gst.Format.TIME)

    def _query_seeking_on_pipeline(self) -> Optional[Gst.Query]:
        q = Gst.Query.new_seeking(Gst.Format.TIME)
        if self._pipeline and self._pipeline.query(q):
            return q
        if self._decode and self._decode.query(q):
            return q
        return None

    @staticmethod
    def _unpack_seeking(q: Gst.Query) -> tuple[bool, bool, int, int]:
        """
        Parse GST_QUERY_SEEKING. Returns (ok, seekable, start_ns, stop_ns).
        stop_ns may be Gst.CLOCK_TIME_NONE for a live / open-ended right edge.
        """
        try:
            t = q.parse_seeking()
        except Exception:
            return False, False, 0, Gst.CLOCK_TIME_NONE
        if not isinstance(t, tuple):
            return False, False, 0, Gst.CLOCK_TIME_NONE
        if len(t) >= 4:
            # (Gst.Format, seekable, seek_start, seek_stop) is the usual GI shape.
            seekable = bool(t[1])
            start = int(t[2])
            stop = int(t[3])
            return True, seekable, start, stop
        if len(t) == 3:
            seekable = bool(t[0])
            start = int(t[1])
            stop = int(t[2])
            return True, seekable, start, stop
        return False, False, 0, Gst.CLOCK_TIME_NONE

    def query_seek_limits_ns(self) -> tuple[bool, bool, int, int]:
        """seekable flag from demuxer; start/stop are the advertised seek window (DVR/VOD)."""
        q = self._query_seeking_on_pipeline()
        if q is None:
            return False, False, 0, Gst.CLOCK_TIME_NONE
        return self._unpack_seeking(q)

    def note_playhead_ns(self, position_ns: int) -> None:
        """Call when you have a valid position so live-edge tracking can extend the scrub range."""
        if position_ns != Gst.CLOCK_TIME_NONE and position_ns >= 0:
            self._live_edge_ns = max(self._live_edge_ns, int(position_ns))

    def get_timeline_for_ui(self, position_ns: int) -> tuple[bool, int, int, bool]:
        """
        Timeline span for the scrubber and end-time label.

        Returns (ok, range_start_ns, range_end_ns, open_live).
        open_live True means the right edge follows max(playhead, live_edge) (typical sliding-window live HLS).

        Live HLS with retained segments often reports seekable=False while still advertising
        seek_start/seek_stop or an open right edge — we still allow scrubbing into the past.
        """
        self.note_playhead_ns(position_ns)

        ok_dur, dur_ns = self.query_duration()
        ok_seek, _seekable, s0, s1 = self.query_seek_limits_ns()

        # Fixed window from demuxer (finite DVR or VOD).
        if ok_seek and s1 != Gst.CLOCK_TIME_NONE and s1 > s0:
            end = max(int(s1), self._live_edge_ns)
            return True, s0, end, False

        # Explicit duration (some live setups still expose playlist depth here).
        # HLS often reports this from the current manifest; it may lag until a bus
        # message—merge with playhead high-water so the scrubber end tracks live growth.
        if ok_dur and dur_ns != Gst.CLOCK_TIME_NONE and dur_ns > 0:
            start = s0 if ok_seek else 0
            du = int(dur_ns)
            if du > start:
                end = max(du, self._live_edge_ns)
                return True, start, end, False

        # Open-ended live: scrub between oldest seekable time and furthest time we've reached.
        if ok_seek and s0 >= 0:
            end = max(int(position_ns), self._live_edge_ns)
            if end > s0:
                return True, s0, end, True

        return False, 0, 0, False

    def scrubbing_allowed(self, position_ns: int) -> bool:
        ok, a, b, _ = self.get_timeline_for_ui(position_ns)
        return ok and b > a

    def is_seekable(self) -> bool:
        ok, seekable, _, _ = self.query_seek_limits_ns()
        return ok and seekable

    def seek_simple(self, position_ns: int) -> bool:
        if not self._pipeline:
            return False
        return self._pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            position_ns,
        )

    def seek_accurate(self, position_ns: int) -> bool:
        if not self._pipeline:
            return False
        return self._pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            position_ns,
        )
