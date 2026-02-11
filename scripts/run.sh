#!/usr/bin/env bash
# Start the full stack: Kafka, Redis, Prometheus, Grafana + Detector
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Check for .env file
if [ ! -f .env ]; then
    echo "WARNING: .env file not found. Copying from .env.example"
    cp .env.example .env
    echo "Please edit .env with your RTSP_URI and other settings."
fi

echo "Starting infrastructure (Kafka, Redis, Prometheus, Grafana)..."
docker compose up -d zookeeper kafka redis prometheus grafana kafka-ui

echo "Waiting for Kafka to be ready..."
sleep 10

echo "Building and starting container-detector..."
docker compose up --build container-detector
