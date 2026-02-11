#!/usr/bin/env bash
# Create Kafka topics for the container detection system
set -euo pipefail

KAFKA_CONTAINER="${1:-kafka}"
BOOTSTRAP="${2:-kafka:9092}"

echo "Creating Kafka topics..."

docker compose exec "$KAFKA_CONTAINER" kafka-topics \
    --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists \
    --topic container-recognition-results \
    --partitions 3 \
    --replication-factor 1

docker compose exec "$KAFKA_CONTAINER" kafka-topics \
    --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists \
    --topic container-recognition-images \
    --partitions 3 \
    --replication-factor 1 \
    --config max.message.bytes=10485760

echo "Topics created:"
docker compose exec "$KAFKA_CONTAINER" kafka-topics \
    --bootstrap-server "$BOOTSTRAP" \
    --list
