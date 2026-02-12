# Implementation Plan

> Auto-maintained by Architect agent. Single source of progress tracking.
> Last updated: 2026-02-12

<!-- TOKENS: ~1200 -->

## Progress

- **Current Phase:** 5 (Guided Implementation — system operational)
- **Current Increment:** INC-08 (complete)
- **Blockers:** none

---

## Architecture Decision Records

### ADR-001: Video Processing Framework
```yaml
ADR-001:
  title: "Video processing framework"
  status: accepted
  context: "Need GPU-accelerated video decoding + inference pipeline"
  options:
    - option: "NVIDIA DeepStream"
      pros: [zero-copy GPU pipeline, built-in tracker, TensorRT integration]
      cons: [NVIDIA lock-in, complex GStreamer API, large Docker image]
    - option: "OpenCV + custom pipeline"
      pros: [portable, simple API]
      cons: [no GPU decode, manual tracking, slow]
    - option: "GStreamer raw + TensorRT"
      pros: [flexible, no framework lock-in]
      cons: [massive custom code, reinventing DeepStream]
  decision: "NVIDIA DeepStream 8.0"
  rationale: "Zero-copy GPU pipeline with built-in tracking is critical for real-time performance. Docker deployment mitigates lock-in concerns."
```

### ADR-002: OCR Engine
```yaml
ADR-002:
  title: "OCR engine for container number recognition"
  status: accepted
  context: "Need to read ISO 6346 codes from video frame crops"
  options:
    - option: "PaddleOCR"
      pros: [GPU acceleration, good accuracy on industrial text, active development]
      cons: [large dependency, PaddlePaddle framework]
    - option: "Tesseract"
      pros: [lightweight, mature, widely available]
      cons: [CPU only, poor on rotated/noisy text]
    - option: "EasyOCR"
      pros: [PyTorch-based, good multilingual]
      cons: [slower than PaddleOCR, less industrial-text accuracy]
  decision: "PaddleOCR with GPU"
  rationale: "Best accuracy on industrial/stencil fonts. GPU support keeps latency under 200ms. Retry mechanism compensates for occasional misreads."
```

### ADR-003: Deduplication Strategy
```yaml
ADR-003:
  title: "Container deduplication approach"
  status: accepted
  context: "Same container visible for seconds — must not report multiple times"
  options:
    - option: "Track-level only (in-memory)"
      pros: [simple, no external dependency]
      cons: [lost on restart, no cross-session dedup]
    - option: "Redis with TTL"
      pros: [persistent across restarts, 24h window, O(1) lookup]
      cons: [external dependency, network latency]
    - option: "Database (PostgreSQL)"
      pros: [queryable history, rich metadata]
      cons: [overkill for dedup, higher latency]
  decision: "Track-level (probe_handler) + Redis 24h TTL + fuzzy matching"
  rationale: "Three-layer approach: track prevents per-frame spam, Redis prevents cross-restart duplicates, fuzzy matching handles OCR variance (O↔0, I↔1)."
```

### ADR-004: Message Bus
```yaml
ADR-004:
  title: "Event publishing mechanism"
  status: accepted
  context: "Detected containers must be published to downstream systems"
  options:
    - option: "Apache Kafka"
      pros: [durable, scalable, replay capability, ecosystem]
      cons: [heavy infrastructure (Zookeeper)]
    - option: "Redis Pub/Sub"
      pros: [already have Redis, simple]
      cons: [no persistence, no replay, at-most-once]
    - option: "NATS"
      pros: [lightweight, fast]
      cons: [less ecosystem, no built-in persistence in core]
  decision: "Apache Kafka (2 topics: results JSON + images base64)"
  rationale: "Durability and replay are essential for logistics — missed events are unacceptable. Kafka-UI simplifies debugging."
```

---

## Components

<!-- STATUS: done -->

```yaml
components:
  - module: Pipeline (deepstream_pipeline.py)
    responsibility: "Build and manage GStreamer/DeepStream processing graph"
    interface:
      - fn: build_pipeline(config) -> Gst.Pipeline
      - fn: start() -> None
      - fn: stop() -> None
    depends_on: []

  - module: ProbeHandler (probe_handler.py)
    responsibility: "Per-frame callback: extract detections, manage OCR retry, publish results"
    interface:
      - fn: on_buffer(pad, info, ctx) -> Gst.PadProbeReturn
    depends_on: [ContainerOCR, KafkaProducer, DeduplicationStore, Metrics]

  - module: ContainerOCR (container_ocr.py)
    responsibility: "Crop, preprocess, OCR, parse ISO numbers and weights"
    interface:
      - fn: recognize(frame: np.ndarray, bbox: Rect) -> ContainerInfo | None
    depends_on: []

  - module: KafkaProducer (producer.py)
    responsibility: "Publish detection events and snapshots to Kafka topics"
    interface:
      - fn: send_result(container_info, metadata) -> None
      - fn: send_image(jpeg_bytes, metadata) -> None
    depends_on: []

  - module: DeduplicationStore (deduplication.py)
    responsibility: "Check and record seen containers with fuzzy matching"
    interface:
      - fn: is_duplicate(iso_number: str) -> bool
      - fn: mark_seen(iso_number: str, metadata: dict) -> None
    depends_on: []

  - module: Metrics (metrics.py)
    responsibility: "Expose Prometheus counters/gauges/histograms"
    interface:
      - fn: start_http_server(port: int) -> None
    depends_on: []

  - module: Config (config.py)
    responsibility: "Load YAML config with env var substitution"
    interface:
      - fn: load_config(path: str) -> dict
    depends_on: []
```

---

## Implementation Roadmap

<!-- STATUS: done -->

```yaml
increments:
  - id: INC-01
    title: "Project scaffold + config loader"
    status: complete
    files:
      - path: src/utils/config.py        (create, ~40 LOC)
      - path: src/utils/logger.py         (create, ~25 LOC)
      - path: config/app_config.yml       (create, ~90 LOC)
    acceptance: "Config loads with env var substitution"

  - id: INC-02
    title: "DeepStream pipeline builder"
    status: complete
    files:
      - path: src/pipeline/deepstream_pipeline.py  (create, ~200 LOC)
      - path: config/ds_detector_config.txt         (create, ~30 LOC)
      - path: config/ds_tracker_config.yml          (create, ~50 LOC)
    depends_on: [INC-01]
    acceptance: "Pipeline starts, decodes RTSP, runs YOLO inference"

  - id: INC-03
    title: "OCR engine + ISO parser"
    status: complete
    files:
      - path: src/ocr/container_ocr.py  (create, ~240 LOC)
    depends_on: []
    acceptance: "recognize() returns valid ContainerInfo from test crop"

  - id: INC-04
    title: "Deduplication store (Redis + Memory)"
    status: complete
    files:
      - path: src/utils/deduplication.py   (create, ~170 LOC)
      - path: tests/test_deduplication.py  (create, ~90 LOC)
    depends_on: []
    acceptance: "Exact + fuzzy dedup works, TTL expires correctly"

  - id: INC-05
    title: "Kafka producer"
    status: complete
    files:
      - path: src/kafka_producer/producer.py  (create, ~120 LOC)
    depends_on: []
    acceptance: "JSON + image messages published to correct topics"

  - id: INC-06
    title: "Probe handler (orchestration)"
    status: complete
    files:
      - path: src/pipeline/probe_handler.py  (create, ~420 LOC)
    depends_on: [INC-02, INC-03, INC-04, INC-05]
    acceptance: "End-to-end: frame → detect → OCR retry → dedup → Kafka"

  - id: INC-07
    title: "Monitoring + health checks"
    status: complete
    files:
      - path: src/monitoring/metrics.py       (create, ~60 LOC)
      - path: src/monitoring/health_check.py  (create, ~20 LOC)
      - path: config/prometheus.yml           (create, ~15 LOC)
      - path: config/grafana/                 (create, dashboards)
    depends_on: [INC-06]
    acceptance: "Prometheus scrapes /metrics, Grafana dashboard loads"

  - id: INC-08
    title: "Docker + compose + scripts"
    status: complete
    files:
      - path: Dockerfile              (create, ~60 LOC)
      - path: docker-compose.yml      (create, ~140 LOC)
      - path: scripts/run.sh          (create, ~25 LOC)
    depends_on: [INC-07]
    acceptance: "docker compose up starts full stack, containers detected"
```

---

## Session Log

> Summarized history of design sessions for context recovery.

### Session 1 — 2026-02-12 (initial)

- Migrated from DeepStream 7.1 to DeepStream 8.0
- Fixed P0-P5 bugs (Redis TTL, O(n)→O(1) dedup, memory leak, exception safety, safety cap)
- All 8 increments implemented and operational
- Ontology and plan documents bootstrapped by Architect agent

### Next Steps

- [ ] Multi-stream support (streammux batching)
- [ ] Model retraining CI/CD pipeline
- [ ] Integration tests with mock RTSP source
- [ ] Edge deployment profile (Jetson Orin)
