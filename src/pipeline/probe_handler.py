"""GStreamer pad probe handler — orchestrates per-frame processing.

Attached to the capsfilter src pad, this probe:
  1. Extracts detected container bounding boxes from DeepStream metadata.
  2. Converts the GPU frame to a CPU numpy array.
  3. Runs PaddleOCR on each detected container region.
  4. Checks deduplication.
  5. Sends unique results + images to Kafka.
  6. Updates Prometheus metrics.
"""

import logging
import os
import time
from datetime import datetime, timezone

import cv2
import numpy as np

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from monitoring.metrics import (
    active_tracks,
    containers_detected_total,
    duplicates_skipped_total,
    pipeline_fps,
    unique_containers_recognized_total,
)

logger = logging.getLogger(__name__)

# Try to import pyds (DeepStream Python bindings)
try:
    import pyds
except ImportError:
    pyds = None
    logger.warning("pyds not available — running in stub mode (no real inference)")


class ProbeHandler:
    """Stateful probe callback — holds references to OCR, dedup, kafka."""

    def __init__(self, ocr_engine, dedup_store, kafka_producer, cfg: dict):
        self.ocr = ocr_engine
        self.dedup = dedup_store
        self.kafka = kafka_producer
        self.cfg = cfg

        self._frame_count = 0
        self._fps_start = time.time()
        self._fps_interval = 30  # recalculate every N frames

        # Track which object IDs have already been OCR-processed
        self._ocr_done_tracks: dict[int, str] = {}  # track_id -> iso_number
        self._track_frame_counts: dict[int, int] = {}  # track_id -> frames seen

        # Minimum frames a tracked object must be visible before OCR
        self._min_frames_for_ocr = 5

        # Snapshot saving
        snap_cfg = cfg.get("snapshots", {})
        self._save_local = snap_cfg.get("save_local", False)
        self._snap_path = snap_cfg.get("local_path", "/app/snapshots")
        self._max_local = snap_cfg.get("max_local_files", 1000)

    def __call__(self, pad, info):
        """GStreamer pad probe callback."""
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        self._frame_count += 1
        self._update_fps()

        if pyds is None:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK

        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            except StopIteration:
                break

            frame_number = frame_meta.frame_num
            n_objects = frame_meta.num_obj_meta

            # Get numpy frame from GPU buffer
            frame_array = self._get_frame_array(gst_buffer, frame_meta)

            track_ids_this_frame = set()
            l_obj = frame_meta.obj_meta_list

            while l_obj is not None:
                try:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                except StopIteration:
                    break

                track_id = obj_meta.object_id
                confidence = obj_meta.confidence
                track_ids_this_frame.add(track_id)

                containers_detected_total.inc()

                # Count frames this track has been visible
                self._track_frame_counts[track_id] = \
                    self._track_frame_counts.get(track_id, 0) + 1

                # Only run OCR if:
                #  - track not yet processed
                #  - object stable enough (seen for N frames)
                if (track_id not in self._ocr_done_tracks
                        and self._track_frame_counts[track_id] >= self._min_frames_for_ocr
                        and frame_array is not None):

                    bbox = (
                        obj_meta.rect_params.left,
                        obj_meta.rect_params.top,
                        obj_meta.rect_params.left + obj_meta.rect_params.width,
                        obj_meta.rect_params.top + obj_meta.rect_params.height,
                    )

                    info = self.ocr.recognize(frame_array, bbox)

                    if info.is_valid():
                        iso = info.iso_number

                        if self.dedup.is_duplicate(iso):
                            duplicates_skipped_total.inc()
                            logger.info("Duplicate container skipped: %s (track=%d)",
                                        iso, track_id)
                            self._ocr_done_tracks[track_id] = iso
                        else:
                            # New unique container
                            self.dedup.mark_seen(iso, info.to_dict())
                            unique_containers_recognized_total.inc()

                            self.kafka.send_result(
                                container_info=info.to_dict(),
                                track_id=track_id,
                                frame_number=frame_number,
                            )
                            self.kafka.send_image(
                                frame=frame_array,
                                bbox=bbox,
                                iso_number=iso,
                                track_id=track_id,
                                frame_number=frame_number,
                            )

                            if self._save_local:
                                self._save_snapshot(frame_array, bbox, iso, frame_number)

                            self._ocr_done_tracks[track_id] = iso
                            logger.info(
                                "New container: %s | gross=%s tare=%s "
                                "(track=%d, frame=%d)",
                                iso,
                                info.max_gross_weight,
                                info.tare_weight,
                                track_id,
                                frame_number,
                            )

                try:
                    l_obj = l_obj.next
                except StopIteration:
                    break

            active_tracks.set(len(track_ids_this_frame))

            # Clean up stale tracks
            self._cleanup_stale_tracks(track_ids_this_frame)

            try:
                l_frame = l_frame.next
            except StopIteration:
                break

        return Gst.PadProbeReturn.OK

    def _get_frame_array(self, gst_buffer, frame_meta) -> np.ndarray | None:
        """Extract a numpy frame from the NVMM buffer."""
        try:
            n_frame = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
            frame = np.array(n_frame, copy=True, order="C")
            # RGBA → BGR for OpenCV
            frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
            return frame
        except Exception:
            logger.debug("Could not extract frame array", exc_info=True)
            return None

    def _cleanup_stale_tracks(self, current_ids: set):
        """Remove tracking state for objects no longer visible."""
        stale = set(self._track_frame_counts.keys()) - current_ids
        for tid in stale:
            self._track_frame_counts.pop(tid, None)
            self._ocr_done_tracks.pop(tid, None)

    def _update_fps(self):
        if self._frame_count % self._fps_interval == 0:
            now = time.time()
            elapsed = now - self._fps_start
            if elapsed > 0:
                fps = self._fps_interval / elapsed
                pipeline_fps.set(round(fps, 1))
            self._fps_start = now

    def _save_snapshot(self, frame: np.ndarray, bbox: tuple,
                       iso_number: str, frame_number: int):
        """Save a local JPEG snapshot of the container."""
        try:
            os.makedirs(self._snap_path, exist_ok=True)
            x1, y1, x2, y2 = [int(v) for v in bbox]
            h, w = frame.shape[:2]
            crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"{ts}_{iso_number}_f{frame_number}.jpg"
            filepath = os.path.join(self._snap_path, filename)
            cv2.imwrite(filepath, crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
            logger.debug("Snapshot saved: %s", filepath)

            # Limit local snapshots
            self._enforce_snapshot_limit()
        except Exception:
            logger.debug("Failed to save snapshot", exc_info=True)

    def _enforce_snapshot_limit(self):
        """Delete oldest snapshots if count exceeds limit."""
        try:
            files = sorted(
                [os.path.join(self._snap_path, f)
                 for f in os.listdir(self._snap_path) if f.endswith(".jpg")],
                key=os.path.getmtime,
            )
            while len(files) > self._max_local:
                os.remove(files.pop(0))
        except Exception:
            pass
