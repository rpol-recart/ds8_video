#!/usr/bin/env python3
"""Test Kafka consumer — reads and prints container recognition messages."""

import json
import sys
from confluent_kafka import Consumer, KafkaError

BOOTSTRAP = sys.argv[1] if len(sys.argv) > 1 else "localhost:29092"
TOPICS = ["container-recognition-results", "container-recognition-images"]

conf = {
    "bootstrap.servers": BOOTSTRAP,
    "group.id": "test-consumer",
    "auto.offset.reset": "earliest",
}

consumer = Consumer(conf)
consumer.subscribe(TOPICS)

print(f"Listening on {TOPICS} @ {BOOTSTRAP}  (Ctrl+C to stop)")

try:
    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            print(f"ERROR: {msg.error()}")
            break

        topic = msg.topic()
        key = msg.key().decode("utf-8") if msg.key() else ""
        value = json.loads(msg.value().decode("utf-8"))

        if topic.endswith("images"):
            # Don't dump the full base64 image
            value["image_base64"] = f"<{len(value.get('image_base64', ''))} chars>"

        print(f"\n{'='*60}")
        print(f"Topic: {topic}  |  Key: {key}")
        print(json.dumps(value, indent=2, ensure_ascii=False))
except KeyboardInterrupt:
    pass
finally:
    consumer.close()
