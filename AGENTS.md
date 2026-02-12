# Project Rules

## Repository: ds8_video

Real-time ISO container number detection from RTSP camera streams using
NVIDIA DeepStream 8, YOLO, PaddleOCR, Kafka, Redis.

## Agent Configuration

This project uses OpenCode AI with custom agents. Config: `opencode.json`.

### Available Agents

| Agent | Mode | Purpose | Invoke |
|-------|------|---------|--------|
| `architect` | primary | Iterative architecture design with file-based memory | `@architect` or default |
| `reviewer` | subagent | Read-only code review against PLAN.md specs | `@reviewer` |

### How the Architect Agent Works

The architect agent is designed for **local LLMs with 64K token context**.
It compensates for limited context by using external file-based memory:

```
┌──────────────────── 64K Context Window ────────────────────┐
│ System   │ ONTOLOGY.md │ PLAN.md  │ Source   │ Response    │
│ ~4K      │ ≤12K        │ ≤12K     │ ≤16K     │ ≤8K         │
└──────────┴─────────────┴──────────┴──────────┴─────────────┘
```

**Memory files** (always re-read at turn start):
- `docs/ONTOLOGY.md` — domain model, glossary, entities, invariants
- `docs/PLAN.md` — ADRs, components, roadmap, session log

**Phases:** Bootstrap → Domain Decomposition → ADRs → Component Design →
Implementation Plan → Guided Implementation

### Quick Start

```bash
# With opencode CLI
opencode                          # uses architect by default (see opencode.json)
opencode agent list               # show available agents

# Or create a new agent interactively
opencode agent create
```

## Code Conventions

- Python 3.12, type hints encouraged
- Functions ≤ 40 lines
- JSON structured logging via `python-json-logger`
- Config: YAML with `${ENV_VAR:-default}` substitution
- Tests: pytest, in `tests/` directory

## File Structure

```
src/
  main.py                    # Entry point
  pipeline/                  # DeepStream GStreamer pipeline
  ocr/                       # PaddleOCR wrapper + ISO parser
  kafka_producer/            # Kafka publisher
  monitoring/                # Prometheus metrics + health check
  utils/                     # Config, dedup, logger
config/                      # YAML/TXT configs, Grafana dashboards
docs/
  ONTOLOGY.md                # Domain ontology (agent-managed)
  PLAN.md                    # Implementation plan (agent-managed)
models/yolo/                 # YOLO ONNX model files
tests/                       # Unit tests
```

## Important Notes

- DeepStream 8 uses TensorRT 10.x — old `.engine` files are incompatible
- `pyds` is pre-installed in the DS8 container, do not pip install it
- Use `--break-system-packages` for pip in Dockerfile (Python 3.12 / Ubuntu 24.04)
- Redis dedup uses fuzzy matching (O/0, I/1 confusion) with max 50 variants
- OCR retry is capped at 200 attempts per track
