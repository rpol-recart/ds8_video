#!/usr/bin/env python3
"""Container Detection System — Main Entry Point.

Builds and starts the DeepStream pipeline with:
  - RTSP source → YOLO container detection → NvDCF tracker
  - PaddleOCR probe for ISO number + weight/capacity recognition
  - Redis-backed deduplication
  - Kafka output (results JSON + container snapshots)
  - Prometheus metrics endpoint
"""

import argparse
import logging
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from utils.config import load_config
from utils.logger import setup_logging
from utils.deduplication import create_dedup_store
from ocr.container_ocr import ContainerOCR
from kafka_producer.producer import ContainerKafkaProducer
from pipeline.deepstream_pipeline import DeepStreamPipeline
from pipeline.probe_handler import ProbeHandler
from monitoring.metrics import start_metrics_server

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Container Detection System (DeepStream + PaddleOCR)",
    )
    parser.add_argument(
        "--config", "-c",
        default="/app/config/app_config.yml",
        help="Path to application YAML config",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # ── Load configuration ───────────────────────────────────
    cfg = load_config(args.config)
    setup_logging(cfg.get("monitoring", {}).get("log_level", "INFO"))

    logger.info("=== Container Detection System starting ===")
    logger.info("RTSP source: %s", cfg["rtsp"]["uri"])

    # ── Start Prometheus metrics server ──────────────────────
    metrics_port = cfg.get("monitoring", {}).get("prometheus_port", 8000)
    start_metrics_server(metrics_port)

    # ── Initialize components ────────────────────────────────
    ocr_engine = ContainerOCR(cfg.get("ocr", {}))
    dedup_store = create_dedup_store(cfg.get("deduplication", {}))
    kafka_prod = ContainerKafkaProducer(cfg.get("kafka", {}))

    # ── Build DeepStream pipeline ────────────────────────────
    ds_pipeline = DeepStreamPipeline(cfg)
    ds_pipeline.build()

    # ── Attach probe to capsfilter src pad ───────────────────
    probe = ProbeHandler(ocr_engine, dedup_store, kafka_prod, cfg)
    pad = ds_pipeline.get_capsfilter_src_pad()
    pad.add_probe(Gst.PadProbeType.BUFFER, probe)
    logger.info("Probe handler attached to capsfilter src pad")

    # ── Graceful shutdown ────────────────────────────────────
    def shutdown(signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        kafka_prod.flush(timeout=10.0)
        ds_pipeline.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Run ──────────────────────────────────────────────────
    ds_pipeline.start()


if __name__ == "__main__":
    main()
