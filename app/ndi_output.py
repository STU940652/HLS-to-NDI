"""Always-running NDI output pipeline fed by intervideosrc / interaudiosrc."""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from app.gst_utils import (
    INTER_AUDIO_CAPS_STR,
    INTER_AUDIO_CHANNEL,
    INTER_VIDEO_CAPS_STR,
    INTER_VIDEO_CHANNEL,
    REQUIRED_NDI_PLUGINS,
    missing_plugins,
)

logger = logging.getLogger(__name__)


class NdiOutputPipeline:
    """
    Separate Gst.Pipeline that stays in PLAYING so NDI subscribers see a stable source.
    Video/audio are bridged from the main playback pipeline via inter* channels.

    Uses ndisinkcombiner so both tracks reach NDI (same layout as::

        ... ! ndisinkcombiner name=combiner ! ndisink ...
        ... ! combiner.audio
    """

    def __init__(
        self,
        ndi_name: str = "GTK_NDI_Player",
        *,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._ndi_name = ndi_name
        self._on_error = on_error
        self._pipeline: Optional[Gst.Pipeline] = None
        self._bus_watch_id: Optional[int] = None

    @property
    def pipeline(self) -> Optional[Gst.Pipeline]:
        return self._pipeline

    def build(self) -> None:
        missing = missing_plugins(REQUIRED_NDI_PLUGINS)
        if missing:
            raise RuntimeError(
                "NDI output unavailable — missing GStreamer elements: "
                + ", ".join(missing)
                + ". Install NDI GStreamer plugins providing `ndisinkcombiner` and `ndisink`."
            )

        pipe = Gst.Pipeline.new("ndi_output_pipeline")

        vsrc = Gst.ElementFactory.make("intervideosrc", "ndi_inter_video_src")
        asrc = Gst.ElementFactory.make("interaudiosrc", "ndi_inter_audio_src")
        vsrc_caps = Gst.ElementFactory.make("capsfilter", "ndi_inter_video_src_caps")
        vsrc_caps.set_property("caps", Gst.Caps.from_string(INTER_VIDEO_CAPS_STR))
        asrc_caps = Gst.ElementFactory.make("capsfilter", "ndi_inter_audio_src_caps")
        asrc_caps.set_property("caps", Gst.Caps.from_string(INTER_AUDIO_CAPS_STR))
        vq = Gst.ElementFactory.make("queue", "ndi_video_queue")
        aq = Gst.ElementFactory.make("queue", "ndi_audio_queue")
        vconvert = Gst.ElementFactory.make("videoconvert", "ndi_vconvert")
        vcaps = Gst.ElementFactory.make("capsfilter", "ndi_video_caps")
        vcaps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=UYVY"))

        combiner = Gst.ElementFactory.make("ndisinkcombiner", "combiner")
        ndisink = Gst.ElementFactory.make("ndisink", "ndi_sink")

        aconvert = Gst.ElementFactory.make("audioconvert", "ndi_aconvert")
        aresample = Gst.ElementFactory.make("audioresample", "ndi_aresample")
        # ndisinkcombiner audio pad expects F32LE interleaved (see gst-inspect-1.0 ndisinkcombiner).
        acaps = Gst.ElementFactory.make("capsfilter", "ndi_audio_caps")
        acaps.set_property(
            "caps",
            Gst.Caps.from_string("audio/x-raw,format=F32LE,layout=interleaved"),
        )

        if not all(
            [
                vsrc,
                asrc,
                vsrc_caps,
                asrc_caps,
                vq,
                aq,
                vconvert,
                vcaps,
                combiner,
                ndisink,
                aconvert,
                aresample,
                acaps,
            ]
        ):
            raise RuntimeError("Failed to create one or more NDI pipeline elements.")

        vsrc.set_property("channel", INTER_VIDEO_CHANNEL)
        vsrc.set_property("timeout", sys.maxsize)
        asrc.set_property("channel", INTER_AUDIO_CHANNEL)

        self._apply_ndisink_properties(ndisink)

        for q in (vq, aq):
            q.set_property("max-size-buffers", 4)
            q.set_property("max-size-time", 200 * Gst.MSECOND)

        for el in (
            vsrc,
            vsrc_caps,
            vq,
            vconvert,
            vcaps,
            combiner,
            ndisink,
            asrc,
            asrc_caps,
            aq,
            aconvert,
            aresample,
            acaps,
        ):
            pipe.add(el)

        if not vsrc.link(vsrc_caps):
            raise RuntimeError("intervideosrc -> capsfilter link failed")
        if not vsrc_caps.link(vq):
            raise RuntimeError("capsfilter -> queue link failed")
        if not vq.link(vconvert):
            raise RuntimeError("queue -> videoconvert link failed")
        if not vconvert.link(vcaps):
            raise RuntimeError("videoconvert -> capsfilter (UYVY) link failed")

        if not asrc.link(asrc_caps):
            raise RuntimeError("interaudiosrc -> capsfilter link failed")
        if not asrc_caps.link(aq):
            raise RuntimeError("capsfilter -> queue link failed")
        if not aq.link(aconvert):
            raise RuntimeError("queue -> audioconvert link failed")
        if not aconvert.link(aresample):
            raise RuntimeError("audioconvert -> audioresample link failed")
        if not aresample.link(acaps):
            raise RuntimeError("audioresample -> capsfilter link failed")

        if not self._link_combiner_chains(vcaps, acaps, combiner, ndisink):
            raise RuntimeError(
                "Could not link ndisinkcombiner / ndisink. Run "
                "`gst-inspect-1.0 ndisinkcombiner` and `gst-inspect-1.0 ndisink`."
            )

        self._pipeline = pipe

    def _apply_ndisink_properties(self, ndisink: Gst.Element) -> None:
        name = self._ndi_name
        for prop in ("ndi-name", "name"):
            try:
                ndisink.set_property(prop, name)
                return
            except Exception:
                continue
        logger.warning("Could not set ndisink display name; using plugin default.")

    def _link_combiner_chains(
        self,
        vcaps: Gst.Element,
        acaps: Gst.Element,
        combiner: Gst.Element,
        ndisink: Gst.Element,
    ) -> bool:
        """video/x-raw UYVY -> combiner.video; audio/x-raw F32LE -> combiner.audio; combiner -> ndisink."""
        vpad = vcaps.get_static_pad("src")
        apad = acaps.get_static_pad("src")

        v_sink = combiner.get_static_pad("video")
        if v_sink is None:
            logger.error("ndisinkcombiner: no static sink pad 'video'")
            return False
        if vpad.link(v_sink) != Gst.PadLinkReturn.OK:
            logger.error("Failed linking video to ndisinkcombiner.video")
            return False

        a_sink = combiner.get_request_pad("audio")
        if a_sink is None:
            a_sink = combiner.get_static_pad("audio")
        if a_sink is None:
            logger.error("ndisinkcombiner: no pad 'audio' (request or static)")
            return False
        if apad.link(a_sink) != Gst.PadLinkReturn.OK:
            logger.error("Failed linking audio to ndisinkcombiner.audio")
            return False

        csrc = combiner.get_static_pad("src")
        if csrc is None:
            logger.error("ndisinkcombiner: no src pad")
            return False

        n_sink = ndisink.get_static_pad("sink")
        if n_sink is None:
            n_sink = ndisink.get_request_pad("sink_%u")
        if n_sink is None:
            n_sink = ndisink.get_request_pad("sink")
        if n_sink is None:
            logger.error("ndisink: no sink pad")
            return False

        if csrc.link(n_sink) != Gst.PadLinkReturn.OK:
            logger.error("Failed linking ndisinkcombiner to ndisink")
            return False

        return True

    def set_ndi_name(self, name: str) -> None:
        self._ndi_name = name
        if not self._pipeline:
            return
        ndisink = self._pipeline.get_by_name("ndi_sink")
        if ndisink:
            self._apply_ndisink_properties(ndisink)

    def start(self) -> None:
        if not self._pipeline:
            self.build()
        assert self._pipeline is not None

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("NDI pipeline failed to start (set_state PLAYING).")

    def stop(self) -> None:
        if self._pipeline:
            bus = self._pipeline.get_bus()
            if self._bus_watch_id is not None:
                bus.disconnect(self._bus_watch_id)
                self._bus_watch_id = None
            bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        mtype = message.type
        if mtype == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            text = f"NDI pipeline error: {err.message} ({dbg})"
            logger.error(text)
            if self._on_error:
                self._on_error(text)
        elif mtype == Gst.MessageType.WARNING:
            warn, dbg = message.parse_warning()
            logger.warning("NDI pipeline warning: %s (%s)", warn.message, dbg)
