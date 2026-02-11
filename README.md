# Container Detection System (DeepStream + PaddleOCR)

Система видеонаблюдения для автоматического распознавания ISO-номеров контейнеров и сопутствующих характеристик (вес, объём) из RTSP-видеопотока.

## Архитектура

```
RTSP Camera
    │
    ▼
┌──────────────────────────────────────────────────┐
│  DeepStream Pipeline                             │
│                                                  │
│  rtspsrc → decode → streammux                    │
│      → nvinfer (YOLO: детекция контейнера)       │
│      → nvtracker (NvDCF: трекинг объектов)       │
│      → nvvideoconvert (RGBA)                     │
│      → [probe callback]                          │
│           │                                      │
│           ├─ PaddleOCR (распознавание текста)     │
│           ├─ Дедупликация (Redis)                 │
│           ├─ Kafka Producer (результаты + фото)   │
│           └─ Prometheus метрики                   │
│                                                  │
│      → fakesink                                  │
└──────────────────────────────────────────────────┘
    │              │               │
    ▼              ▼               ▼
  Kafka        Prometheus       Redis
(результаты   (мониторинг)   (дедупликация)
 + снимки)
    │              │
    ▼              ▼
Внешняя        Grafana
система       (дашборды)
```

## Распознаваемые данные

Для каждого контейнера система извлекает:

| Поле | Пример | Описание |
|------|--------|----------|
| ISO номер | `MSCU1234567` | Код владельца (4 буквы) + серийный номер (6 цифр) + контрольная цифра |
| Max Gross Weight | `30480 KG` | Максимальная масса брутто |
| Tare Weight | `2200 KG` | Масса тары |
| Net Weight / Payload | `28280 KG` | Масса нетто / полезная нагрузка |
| Cube Capacity | `33.2 CBM` | Объём |
| Raw Lines | — | Все распознанные строки текста (4-6 строк) |

## Требования

- NVIDIA GPU (Compute Capability ≥ 7.0)
- Docker + Docker Compose + NVIDIA Container Toolkit
- RTSP-камера

## Быстрый старт

### 1. Настройка

```bash
cp .env.example .env
# Отредактируйте .env — укажите RTSP_URI вашей камеры
```

### 2. Модель детекции

Поместите обученную YOLO модель (ONNX) для детекции контейнеров:

```bash
cp your_model.onnx models/yolo/best.onnx
```

Модель должна детектировать один класс — `container`.

### 3. Запуск

```bash
./scripts/run.sh
```

Или вручную:

```bash
# Инфраструктура
docker compose up -d zookeeper kafka redis prometheus grafana

# Приложение
docker compose up --build container-detector
```

### 4. Проверка

```bash
# Чтение результатов из Kafka
python3 scripts/test_kafka_consumer.py localhost:29092

# Grafana дашборд
open http://localhost:3000  # admin / admin

# Prometheus метрики
open http://localhost:9090

# Kafka UI
open http://localhost:8080
```

## Структура проекта

```
├── config/
│   ├── app_config.yml            # Основная конфигурация
│   ├── ds_detector_config.txt    # Конфиг DeepStream nvinfer
│   ├── ds_tracker_config.yml     # Конфиг NvDCF трекера
│   ├── labels.txt                # Метки классов
│   ├── prometheus.yml            # Конфиг Prometheus
│   └── grafana/                  # Дашборды и datasources
├── models/
│   └── yolo/                     # YOLO ONNX модель
├── src/
│   ├── main.py                   # Точка входа
│   ├── pipeline/
│   │   ├── deepstream_pipeline.py  # Построение GStreamer-пайплайна
│   │   └── probe_handler.py        # Probe callback (OCR + Kafka + dedup)
│   ├── ocr/
│   │   └── container_ocr.py        # PaddleOCR интеграция
│   ├── kafka_producer/
│   │   └── producer.py             # Отправка в Kafka
│   ├── monitoring/
│   │   ├── metrics.py              # Prometheus метрики
│   │   └── health_check.py         # Docker HEALTHCHECK
│   └── utils/
│       ├── config.py               # Загрузка YAML-конфигурации
│       ├── deduplication.py        # Redis / memory дедупликация
│       └── logger.py               # Структурированное JSON-логирование
├── scripts/
│   ├── run.sh                    # Запуск всего стека
│   ├── create_kafka_topics.sh    # Создание топиков Kafka
│   └── test_kafka_consumer.py    # Тестовый потребитель Kafka
├── tests/
│   └── test_deduplication.py     # Юнит-тесты дедупликации
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Kafka-сообщения

### Топик `container-recognition-results`

```json
{
  "event_type": "container_recognized",
  "timestamp": "2026-02-11T12:00:00.000Z",
  "source_id": "cam-0",
  "frame_number": 1500,
  "track_id": 3,
  "container": {
    "iso_number": "MSCU1234567",
    "owner_code": "MSCU",
    "serial_number": "123456",
    "check_digit": "7",
    "max_gross_weight": "30480 KG",
    "tare_weight": "2200 KG",
    "net_weight": "28280 KG",
    "payload": null,
    "cube_capacity": "33.2 CBM",
    "raw_lines": ["MSCU 123456-7", "MAX GROSS 30480 KG", "TARE 2200 KG", "NET 28280 KG", "CU CAP 33.2 CBM"],
    "confidence": 0.92
  }
}
```

### Топик `container-recognition-images`

```json
{
  "event_type": "container_snapshot",
  "timestamp": "2026-02-11T12:00:00.000Z",
  "iso_number": "MSCU1234567",
  "track_id": 3,
  "frame_number": 1500,
  "image_format": "jpeg",
  "image_base64": "<base64-encoded JPEG>"
}
```

## Дедупликация

Система предотвращает повторную отправку одного и того же контейнера:

1. **Трекинг**: NvDCF трекер присваивает каждому объекту уникальный `track_id`. OCR выполняется однократно для каждого трека.
2. **Redis TTL**: Распознанный ISO-номер сохраняется в Redis с TTL (по умолчанию 5 минут). Повторное появление того же номера в течение TTL игнорируется.
3. **Fuzzy matching**: Нечёткое сравнение (Levenshtein, порог 0.85) предотвращает дубли при незначительных ошибках OCR.

## Мониторинг

Prometheus метрики доступны на `http://container-detector:8000/metrics`:

| Метрика | Тип | Описание |
|---------|-----|----------|
| `containers_detected_total` | Counter | Всего детекций контейнеров |
| `ocr_recognitions_total` | Counter | Всего попыток OCR |
| `ocr_recognitions_success_total` | Counter | Успешных распознаваний ISO-номера |
| `unique_containers_recognized_total` | Counter | Уникальных контейнеров отправлено |
| `duplicates_skipped_total` | Counter | Пропущено дубликатов |
| `kafka_messages_sent_total` | Counter | Сообщений отправлено в Kafka |
| `kafka_errors_total` | Counter | Ошибок отправки в Kafka |
| `pipeline_fps` | Gauge | Текущий FPS пайплайна |
| `ocr_processing_duration_seconds` | Histogram | Время обработки OCR |

Grafana дашборд предоставляется автоматически: `http://localhost:3000`.

## Конфигурация

Все параметры настраиваются через `config/app_config.yml` и переменные окружения (см. `.env.example`). Переменные окружения подставляются через синтаксис `${VAR:-default}` в YAML.
