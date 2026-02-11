"""DeepStream 8.x / GStreamer pipeline for container detection.

Pipeline structure:
  rtspsrc → rtph264depay → nvv4l2decoder → nvstreammux
    → nvinfer (YOLO container detector)
    → nvtracker
    → nvvideoconvert → capsfilter (RGBA)
    → [probe: OCR + Kafka + dedup]
    → fakesink

DS8 compatibility notes:
  - pyds API (cast(), hash(), linked list iteration) unchanged from DS7.
  - request_pad_simple() used (get_request_pad() deprecated since GStreamer 1.20).
  - TensorRT 10.x engines are NOT compatible with TRT 8.x — delete cached
    .engine files when migrating from DS7.
"""

import logging
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst

logger = logging.getLogger(__name__)

Gst.init(None)


def _make_element(factory_name: str, name: str):
    """Create a GStreamer element or die trying."""
    elem = Gst.ElementFactory.make(factory_name, name)
    if elem is None:
        logger.error("Failed to create element: %s (%s)", name, factory_name)
        sys.exit(1)
    return elem


class DeepStreamPipeline:
    """Builds and manages the GStreamer/DeepStream pipeline."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pipeline = None
        self.loop = None
        self._frame_count = 0
        self._fps_start = time.time()

    def build(self) -> Gst.Pipeline:
        """Construct the full pipeline from config."""
        pipeline = Gst.Pipeline.new("container-detection-pipeline")

        # ── Source: RTSP ─────────────────────────────────────
        source = _make_element("rtspsrc", "rtsp-source")
        source.set_property("location", self.cfg["rtsp"]["uri"])
        source.set_property("latency", self.cfg["rtsp"].get("latency", 200))
        source.set_property("drop-on-latency", True)

        # ── Depay + Decode ───────────────────────────────────
        depay = _make_element("rtph264depay", "depay")
        h264parse = _make_element("h264parse", "h264-parse")
        decoder = _make_element("nvv4l2decoder", "nvv4l2-decoder")

        # ── Stream Mux ───────────────────────────────────────
        mux_cfg = self.cfg["pipeline"]["streammux"]
        streammux = _make_element("nvstreammux", "stream-mux")
        streammux.set_property("batch-size", mux_cfg.get("batch_size", 1))
        streammux.set_property("width", mux_cfg.get("width", 1920))
        streammux.set_property("height", mux_cfg.get("height", 1080))
        streammux.set_property("batched-push-timeout", mux_cfg.get("batched_push_timeout", 40000))
        streammux.set_property("live-source", mux_cfg.get("live_source", True))

        # ── Primary Inference (YOLO container detector) ──────
        det_cfg = self.cfg["pipeline"]["detector"]
        pgie = _make_element("nvinfer", "primary-inference")
        pgie.set_property("config-file-path", det_cfg["config_file"])
        pgie.set_property("batch-size", det_cfg.get("batch_size", 1))

        # ── Tracker ──────────────────────────────────────────
        trk_cfg = self.cfg["pipeline"]["tracker"]
        tracker = _make_element("nvtracker", "tracker")
        tracker.set_property("tracker-width", trk_cfg.get("tracker_width", 960))
        tracker.set_property("tracker-height", trk_cfg.get("tracker_height", 544))
        tracker.set_property("ll-lib-file", trk_cfg["ll_lib_file"])
        tracker.set_property("ll-config-file", trk_cfg["ll_config_file"])

        # ── Video Convert + Caps (RGBA for frame access) ────
        nvvidconv = _make_element("nvvideoconvert", "nvvideo-converter")
        capsfilter = _make_element("capsfilter", "caps-filter")
        caps = Gst.Caps.from_string("video/x-raw(memory:NVMM), format=RGBA")
        capsfilter.set_property("caps", caps)

        # ── Sink ─────────────────────────────────────────────
        sink = _make_element("fakesink", "fakesink")
        sink.set_property("sync", 0)
        sink.set_property("async", 0)

        # ── Add elements to pipeline ─────────────────────────
        for elem in [source, depay, h264parse, decoder, streammux,
                     pgie, tracker, nvvidconv, capsfilter, sink]:
            pipeline.add(elem)

        # ── Link static elements ─────────────────────────────
        depay.link(h264parse)
        h264parse.link(decoder)

        # Decoder → streammux pad
        srcpad = decoder.get_static_pad("src")
        sinkpad = streammux.request_pad_simple("sink_0")
        if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
            logger.error("Failed to link decoder → streammux")
            sys.exit(1)

        streammux.link(pgie)
        pgie.link(tracker)
        tracker.link(nvvidconv)
        nvvidconv.link(capsfilter)
        capsfilter.link(sink)

        # ── Dynamic pad linking for rtspsrc ──────────────────
        source.connect("pad-added", self._on_rtspsrc_pad_added, depay)

        self.pipeline = pipeline
        logger.info("DeepStream pipeline built successfully")
        return pipeline

    @staticmethod
    def _on_rtspsrc_pad_added(src, new_pad, depay):
        """Handle dynamic pad creation from rtspsrc."""
        sink_pad = depay.get_static_pad("sink")
        if sink_pad.is_linked():
            return
        caps = new_pad.get_current_caps()
        struct = caps.get_structure(0)
        name = struct.get_name()
        if name.startswith("application/x-rtp"):
            result = new_pad.link(sink_pad)
            if result == Gst.PadLinkReturn.OK:
                logger.info("RTSP source pad linked to depay")
            else:
                logger.error("Failed to link RTSP pad: %s", result)

    def get_tracker_src_pad(self) -> Gst.Pad:
        """Return the tracker's src pad for attaching probes."""
        tracker = self.pipeline.get_by_name("tracker")
        return tracker.get_static_pad("src")

    def get_capsfilter_src_pad(self) -> Gst.Pad:
        """Return capsfilter src pad for attaching frame-access probes."""
        cf = self.pipeline.get_by_name("caps-filter")
        return cf.get_static_pad("src")

    def start(self):
        """Set pipeline to PLAYING state and run the main loop."""
        self.loop = GLib.MainLoop()

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.error("Unable to set pipeline to PLAYING")
            sys.exit(1)

        logger.info("Pipeline started — PLAYING")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self.stop()

    def stop(self):
        """Stop the pipeline gracefully."""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            logger.info("Pipeline stopped")
        if self.loop and self.loop.is_running():
            self.loop.quit()

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("End of stream")
            self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error("Pipeline error: %s (debug: %s)", err, debug)
            self.loop.quit()
        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            logger.warning("Pipeline warning: %s (debug: %s)", err, debug)
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old, new, _ = message.parse_state_changed()
                logger.debug("Pipeline state: %s → %s",
                             old.value_nick, new.value_nick)
