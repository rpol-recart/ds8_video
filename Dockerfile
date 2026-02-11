FROM nvcr.io/nvidia/deepstream:8.0-triton-multiarch

ENV DEBIAN_FRONTEND=noninteractive
ENV DS_VERSION=8.0

# DS8 ships Ubuntu 24.04 + Python 3.12; pip requires --break-system-packages
# inside a container where we own the full filesystem.

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-dev \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gst-rtsp-server-1.0 \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libglib2.0-dev \
    libcairo2-dev \
    libgirepository1.0-dev \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# PaddleOCR dependencies
RUN pip3 install --no-cache-dir --break-system-packages \
    paddlepaddle-gpu \
    paddleocr

# DeepStream Python bindings (pyds) are pre-installed in DS8 container at
# /opt/nvidia/deepstream/deepstream/lib.  Install cuda-python only.
RUN pip3 install --no-cache-dir --break-system-packages cuda-python

WORKDIR /app

# Copy application code
COPY config/ /app/config/
COPY models/ /app/models/
COPY src/ /app/src/
COPY scripts/ /app/scripts/

# Download default YOLO model for container detection if not present
RUN mkdir -p /app/models/yolo && \
    if [ ! -f /app/models/yolo/best.onnx ]; then \
        echo "Place your trained container detection ONNX model at /app/models/yolo/best.onnx"; \
    fi

# NOTE: TensorRT 10.x (DS8) is NOT compatible with TRT 8.x engine files.
# You MUST regenerate .engine files on first run or delete old cached engines.

ENV PYTHONPATH=/app/src:${PYTHONPATH}
ENV GST_DEBUG=2

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 /app/src/monitoring/health_check.py || exit 1

ENTRYPOINT ["python3", "/app/src/main.py"]
