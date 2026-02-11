"""GStreamer pad probe handler — orchestrates per-frame processing.

Attached to the capsfilter src pad, this probe:
  1. Extracts detected container bounding boxes from DeepStream metadata.
  2. Converts the GPU frame to a CPU numpy array.
  3. Runs PaddleOCR on each detected container region.
  4. Checks deduplication.
  5. Sends unique results + images to Kafka.
  6. Updates Prometheus metrics.

OCR retry strategy:
  - Each track gets up to `max_ocr_attempts` tries spaced by `ocr_retry_interval` frames.
  - Each attempt's result is accumulated; the best result (highest confidence,
    valid ISO preferred) is selected when retries are exhausted.
  - If no valid ISO is found after all attempts, the best partial result
    (raw text lines) is sent as a degraded event so downstream can act on it.
"""

import logging
import os
import time
from dataclasses import dataclass, field
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
    ocr_partial_results_sent_total,
    ocr_retries_exhausted_total,
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


@dataclass
class TrackOCRState:
    """Mutable state for OCR retry logic per tracked object."""
    attempts: int = 0
    last_attempt_frame: int = 0
    best_result: object = None      # ContainerInfo or None
    best_confidence: float = 0.0
    finished: bool = False           # True → no more OCR calls for this track
    iso_number: str | None = None    # set once a valid result is accepted


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

        # Per-track OCR state
        self._track_states: dict[int, TrackOCRState] = {}
        self._track_frame_counts: dict[int, int] = {}

        # ── OCR retry configuration ─────────────────────────
        ocr_retry_cfg = cfg.get("ocr", {}).get("retry", {})
        self._min_frames_for_ocr = ocr_retry_cfg.get("min_frames_before_first", 5)
        self._max_ocr_attempts = ocr_retry_cfg.get("max_attempts", 5)
        self._ocr_retry_interval = ocr_retry_cfg.get("frame_interval", 10)

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
                track_ids_this_frame.add(track_id)

                containers_detected_total.inc()

                # Count frames this track has been visible
                self._track_frame_counts[track_id] = \
                    self._track_frame_counts.get(track_id, 0) + 1

                # Initialize OCR state for new tracks
                if track_id not in self._track_states:
                    self._track_states[track_id] = TrackOCRState()

                state = self._track_states[track_id]

                # Skip if already finished (success or exhausted retries)
                if state.finished:
                    try:
                        l_obj = l_obj.next
                    except StopIteration:
                        break
                    continue

                # Skip if not yet stable enough
                if self._track_frame_counts[track_id] < self._min_frames_for_ocr:
                    try:
                        l_obj = l_obj.next
                    except StopIteration:
                        break
                    continue

                # Skip if too soon since last attempt (throttle)
                frames_since_last = frame_number - state.last_attempt_frame
                if state.attempts > 0 and frames_since_last < self._ocr_retry_interval:
                    try:
                        l_obj = l_obj.next
                    except StopIteration:
                        break
                    continue

                # Skip if no frame available
                if frame_array is None:
                    try:
                        l_obj = l_obj.next
                    except StopIteration:
                        break
                    continue

                bbox = (
                    obj_meta.rect_params.left,
                    obj_meta.rect_params.top,
                    obj_meta.rect_params.left + obj_meta.rect_params.width,
                    obj_meta.rect_params.top + obj_meta.rect_params.height,
                )

                # ── Run OCR attempt ──────────────────────────
                ocr_info = self.ocr.recognize(frame_array, bbox)
                state.attempts += 1
                state.last_attempt_frame = frame_number

                # Keep the best result across all attempts
                if ocr_info.confidence > state.best_confidence:
                    state.best_confidence = ocr_info.confidence
                    state.best_result = ocr_info

                logger.debug(
                    "OCR attempt %d/%d for track=%d: valid=%s conf=%.2f",
                    state.attempts, self._max_ocr_attempts,
                    track_id, ocr_info.is_valid(), ocr_info.confidence,
                )

                if ocr_info.is_valid():
                    # ── Success: valid ISO number found ──────
                    self._accept_result(
                        state, ocr_info, track_id, frame_number,
                        frame_array, bbox,
                    )
                elif state.attempts >= self._max_ocr_attempts:
                    # ── Retries exhausted ────────────────────
                    self._handle_exhausted_retries(
                        state, track_id, frame_number,
                        frame_array, bbox,
                    )
                # else: will retry on a later frame

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

    # ── Result handling ──────────────────────────────────────

    def _accept_result(self, state: TrackOCRState, ocr_info,
                       track_id: int, frame_number: int,
                       frame_array: np.ndarray, bbox: tuple):
        """Handle a successful OCR result (valid ISO number)."""
        iso = ocr_info.iso_number
        state.finished = True
        state.iso_number = iso

        if self.dedup.is_duplicate(iso):
            duplicates_skipped_total.inc()
            logger.info("Duplicate container skipped: %s (track=%d, attempt=%d)",
                        iso, track_id, state.attempts)
            return

        # New unique container
        self.dedup.mark_seen(iso, ocr_info.to_dict())
        unique_containers_recognized_total.inc()

        self.kafka.send_result(
            container_info=ocr_info.to_dict(),
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

        logger.info(
            "Container recognized: %s | gross=%s tare=%s "
            "(track=%d, frame=%d, attempt=%d/%d)",
            iso,
            ocr_info.max_gross_weight,
            ocr_info.tare_weight,
            track_id,
            frame_number,
            state.attempts,
            self._max_ocr_attempts,
        )

    def _handle_exhausted_retries(self, state: TrackOCRState,
                                  track_id: int, frame_number: int,
                                  frame_array: np.ndarray, bbox: tuple):
        """Handle a track where all OCR attempts failed to produce a valid ISO."""
        state.finished = True
        ocr_retries_exhausted_total.inc()

        best = state.best_result
        if best is not None and best.raw_lines:
            # Send partial result — downstream may still use raw text
            ocr_partial_results_sent_total.inc()
            partial_dict = best.to_dict()
            partial_dict["recognition_status"] = "partial"

            self.kafka.send_result(
                container_info=partial_dict,
                track_id=track_id,
                frame_number=frame_number,
            )
            self.kafka.send_image(
                frame=frame_array,
                bbox=bbox,
                iso_number=None,
                track_id=track_id,
                frame_number=frame_number,
            )

            logger.warning(
                "OCR retries exhausted for track=%d: no valid ISO. "
                "Sent best partial result (confidence=%.2f, lines=%d). "
                "Raw: %s",
                track_id, best.confidence, len(best.raw_lines),
                best.raw_lines[:3],
            )
        else:
            logger.warning(
                "OCR retries exhausted for track=%d: no text recognized at all "
                "after %d attempts",
                track_id, state.attempts,
            )

    # ── Internals ────────────────────────────────────────────

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
            self._track_states.pop(tid, None)

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
