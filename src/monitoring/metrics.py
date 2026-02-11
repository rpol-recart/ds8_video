"""Prometheus metrics definitions for the container detection system."""

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import logging

logger = logging.getLogger(__name__)

# ── Counters ─────────────────────────────────────────────────
containers_detected_total = Counter(
    "containers_detected_total",
    "Total number of containers detected by the model",
)
ocr_recognitions_total = Counter(
    "ocr_recognitions_total",
    "Total OCR recognition attempts",
)
ocr_recognitions_success = Counter(
    "ocr_recognitions_success_total",
    "Successful OCR recognitions (ISO number extracted)",
)
unique_containers_recognized_total = Counter(
    "unique_containers_recognized_total",
    "Unique containers successfully recognized and sent to Kafka",
)
duplicates_skipped_total = Counter(
    "duplicates_skipped_total",
    "Containers skipped due to deduplication",
)
kafka_messages_sent_total = Counter(
    "kafka_messages_sent_total",
    "Total messages sent to Kafka",
    ["topic"],
)
kafka_errors_total = Counter(
    "kafka_errors_total",
    "Total Kafka send errors",
)
ocr_retries_exhausted_total = Counter(
    "ocr_retries_exhausted_total",
    "Tracks where OCR max retries were exhausted without valid ISO number",
)
ocr_partial_results_sent_total = Counter(
    "ocr_partial_results_sent_total",
    "Tracks where OCR sent best partial result (no ISO but had some text)",
)

# ── Gauges ───────────────────────────────────────────────────
pipeline_fps = Gauge(
    "pipeline_fps",
    "Current pipeline frames per second",
)
active_tracks = Gauge(
    "active_tracks",
    "Number of active object tracks",
)

# ── Histograms ───────────────────────────────────────────────
ocr_processing_duration = Histogram(
    "ocr_processing_duration_seconds",
    "OCR processing time per frame",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
detection_processing_duration = Histogram(
    "detection_processing_duration_seconds",
    "Detection inference time per frame",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)


def start_metrics_server(port: int = 8000):
    """Start the Prometheus metrics HTTP server."""
    start_http_server(port)
    logger.info("Prometheus metrics server started on port %d", port)
