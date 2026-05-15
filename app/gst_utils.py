"""GStreamer helpers: plugin checks, decode-bin selection, time formatting."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

# Shared inter-* channel names (must match between playback and NDI pipelines).
INTER_VIDEO_CHANNEL = "gtk_ndi_player_video"
INTER_AUDIO_CHANNEL = "gtk_ndi_player_audio"
INTER_VIDEO_CAPS_STR = "video/x-raw,width=1920,height=1080,framerate=60/1"
INTER_AUDIO_CAPS_STR = "audio/x-raw,channels=2,rate=48000"

# Minimum plugins required for the full app (NDI plugin may be optional for preview-only testing).
REQUIRED_PLAYBACK_PLUGINS = (
    "queue",
    "tee",
    "videoconvert",
    "videorate",
    "videoscale",
    "audioconvert",
    "audioresample",
    "intervideosink",
    "interaudiosink",
    "intervideosrc",
    "interaudiosrc",
)

REQUIRED_NDI_PLUGINS = ("ndisinkcombiner", "ndisink")

NDI_SDK_DOWNLOAD_URL = "https://ndi.video/for-developers/ndi-sdk/download/"


def plugin_available(factory_name: str) -> bool:
    return Gst.ElementFactory.find(factory_name) is not None


def missing_plugins(names: Iterable[str]) -> List[str]:
    return [n for n in names if not plugin_available(n)]


def ndi_sdk_runtime_probe_error() -> Optional[str]:
    """
    If NDI GStreamer plugins are present, verify the native NDI SDK/runtime loads by
    driving ndisink to PLAYING on a tiny test pipeline.

    Returns None when NDI plugins are missing (not an SDK issue), when the probe
    succeeds, or when the outcome is unclear. Returns a human-readable error when
    the SDK clearly fails to load (e.g. “Failed loading NDI SDK”).
    """
    if missing_plugins(REQUIRED_NDI_PLUGINS):
        return None

    pipe = Gst.Pipeline.new("ndi_sdk_probe")
    try:
        src = Gst.ElementFactory.make("videotestsrc", "probe_src")
        c = Gst.ElementFactory.make("videoconvert", "probe_vc")
        cf = Gst.ElementFactory.make("capsfilter", "probe_caps")
        sink = Gst.ElementFactory.make("ndisink", "probe_ndi")
        if not all((src, c, cf, sink)):
            return "Could not build NDI SDK probe pipeline (missing elements)."
        cf.set_property("caps", Gst.Caps.from_string("video/x-raw,format=UYVY"))
        for el in (src, c, cf, sink):
            pipe.add(el)
        if not src.link(c) or not c.link(cf) or not cf.link(sink):
            return "Could not link NDI SDK probe pipeline."

        for prop in ("ndi-name", "name"):
            try:
                sink.set_property(prop, "GTK_NDI_SDK_Probe")
                break
            except Exception:
                continue

        bus = pipe.get_bus()
        ret = pipe.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            msg = bus.timed_pop_filtered(Gst.SECOND * 2, Gst.MessageType.ERROR)
            if msg is not None and msg.type == Gst.MessageType.ERROR:
                err, _dbg = msg.parse_error()
                return err.message
            return "NDI sink failed to start (set_state PLAYING). The NDI SDK runtime may be missing."

        msg = bus.timed_pop_filtered(Gst.SECOND * 5, Gst.MessageType.ERROR)
        if msg is not None and msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            return err.message

        state_ret, state, pending = pipe.get_state(5 * Gst.SECOND)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            return "Could not query NDI probe pipeline state (NDI SDK runtime may be missing)."
        if state != Gst.State.PLAYING or pending != Gst.State.VOID_PENDING:
            return "NDI sink did not reach PLAYING (NDI SDK runtime may be missing)."
        return None
    finally:
        pipe.set_state(Gst.State.NULL)


def try_make_element(candidates: Iterable[str], name: Optional[str] = None) -> Optional[Gst.Element]:
    for factory in candidates:
        el = Gst.ElementFactory.make(factory, name)
        if el is not None:
            return el
    return None


_decode_bin_available: Optional[bool] = None


def decode_bin_available() -> bool:
    """Return True if uridecodebin3 can be instantiated."""
    global _decode_bin_available
    if _decode_bin_available is not None:
        return _decode_bin_available
    el = try_make_element(("uridecodebin3",), None)
    _decode_bin_available = el is not None
    return _decode_bin_available


def decode_bin_element(name: str = "decode") -> Gst.Element:
    """Always use uridecodebin3 for URI decoding."""
    el = Gst.ElementFactory.make("uridecodebin3", name)
    if el is None:
        raise RuntimeError(
            "uridecodebin3 is not available. Install a GStreamer build that includes "
            "uridecodebin3 (typically gst-plugins-good / base with adaptive streaming support)."
        )
    return el


_TIME_RE = re.compile(
    r"^(?:(?P<h>\d+):)?(?:(?P<m>\d+):)?(?P<s>\d+)(?:\.(?P<ms>\d{1,3}))?$"
)


def parse_time_string(s: str) -> Optional[int]:
    """
    Parse a human time string into nanoseconds.
    Accepts: SS, SS.mmm, MM:SS, MM:SS.mmm, H:MM:SS, H:MM:SS.mmm
    """
    s = s.strip()
    if not s:
        return None
    m = _TIME_RE.match(s)
    if not m:
        return None
    h = int(m.group("h") or 0)
    mi = int(m.group("m") or 0)
    sec = int(m.group("s"))
    ms_str = m.group("ms")
    if ms_str:
        ms = int(ms_str.ljust(3, "0")[:3])
    else:
        ms = 0
    total_sec = h * 3600 + mi * 60 + sec + ms / 1000.0
    return int(total_sec * Gst.SECOND)


def format_ns(ns: int) -> str:
    """Format Gst CLOCK_TIME to HH:MM:SS.mmm (fixed 3 fractional digits)."""
    if ns < 0 or ns == Gst.CLOCK_TIME_NONE:
        return "--:--:--.---"
    ms_total = ns // (Gst.SECOND // 1000)
    ms = ms_total % 1000
    s = (ms_total // 1000) % 60
    m = (ms_total // (1000 * 60)) % 60
    h = ms_total // (1000 * 60 * 60)
    return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"
