"""Kafka producer for sending container recognition results and images."""

import base64
import json
import logging
import time
from datetime import datetime, timezone

import cv2
import numpy as np
from confluent_kafka import Producer

from monitoring.metrics import kafka_errors_total, kafka_messages_sent_total

logger = logging.getLogger(__name__)


class ContainerKafkaProducer:
    """Sends recognition results and container snapshots to Kafka."""

    def __init__(self, cfg: dict):
        self.topic_results = cfg.get("topic_results", "container-recognition-results")
        self.topic_images = cfg.get("topic_images", "container-recognition-images")
        self.image_quality = cfg.get("image_quality", 85)

        producer_conf = {
            "bootstrap.servers": cfg.get("bootstrap_servers", "kafka:9092"),
            "compression.type": cfg.get("compression_type", "gzip"),
            "message.max.bytes": cfg.get("max_message_bytes", 10485760),
            "acks": str(cfg.get("acks", "all")),
            "retries": cfg.get("retries", 3),
            "linger.ms": 50,
            "batch.num.messages": 10,
        }
        self.producer = Producer(producer_conf)
        logger.info("Kafka producer initialized (servers=%s)", producer_conf["bootstrap.servers"])

    @staticmethod
    def _delivery_callback(err, msg):
        if err:
            kafka_errors_total.inc()
            logger.error("Kafka delivery failed: %s", err)
        else:
            topic = msg.topic()
            kafka_messages_sent_total.labels(topic=topic).inc()
            logger.debug("Message delivered to %s [%d] @ %d",
                         topic, msg.partition(), msg.offset())

    def send_result(self, container_info: dict, track_id: int,
                    frame_number: int, source_id: str = "cam-0"):
        """Send recognition result as JSON to the results topic."""
        message = {
            "event_type": "container_recognized",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_id": source_id,
            "frame_number": frame_number,
            "track_id": track_id,
            "container": container_info,
        }

        key = container_info.get("iso_number", f"track-{track_id}")

        try:
            self.producer.produce(
                topic=self.topic_results,
                key=key.encode("utf-8"),
                value=json.dumps(message).encode("utf-8"),
                callback=self._delivery_callback,
            )
            self.producer.poll(0)
            logger.info("Result sent to Kafka: %s (track=%d)", key, track_id)
        except Exception:
            kafka_errors_total.inc()
            logger.exception("Failed to send result to Kafka")

    def send_image(self, frame: np.ndarray, bbox: tuple,
                   iso_number: str, track_id: int, frame_number: int):
        """Send cropped container image to the images topic."""
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            h, w = frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            crop = frame[y1:y2, x1:x2]

            _, buf = cv2.imencode(
                ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, self.image_quality]
            )
            img_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

            message = {
                "event_type": "container_snapshot",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "iso_number": iso_number or f"unknown-track-{track_id}",
                "track_id": track_id,
                "frame_number": frame_number,
                "image_format": "jpeg",
                "image_base64": img_b64,
            }

            key = (iso_number or f"track-{track_id}").encode("utf-8")

            self.producer.produce(
                topic=self.topic_images,
                key=key,
                value=json.dumps(message).encode("utf-8"),
                callback=self._delivery_callback,
            )
            self.producer.poll(0)
        except Exception:
            kafka_errors_total.inc()
            logger.exception("Failed to send image to Kafka")

    def flush(self, timeout: float = 5.0):
        remaining = self.producer.flush(timeout)
        if remaining > 0:
            logger.warning("Kafka flush: %d messages still in queue", remaining)
