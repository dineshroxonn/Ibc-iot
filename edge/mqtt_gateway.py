#!/usr/bin/env python3
"""SensorChain edge gateway agent — MQTT in, Fabric anchors out.

The production-shaped ingestion path: sensors publish readings to the
city's MQTT broker; this agent (running on Raspberry Pi-class gateway
hardware) subscribes, buffers readings into fixed windows, builds the
Merkle tree, signs the anchor payload with the gateway HSM, and submits
one small anchor per window to the Fabric shard through the local
bridge. Full readings are retained in a local batch store (the "city
data platform" copy) from which Merkle proofs are served.

Deliberately lean: no numpy, no ML, no Fabric SDK — just MQTT, SHA-256,
and one ECDSA signature per window. The process measures its own
footprint (CPU%, peak RSS) and prints it on exit, so the <2% CPU /
<50 MB RAM target from the proposal is a measured number, not a claim.

Usage:
  python3 edge/mqtt_gateway.py --broker localhost:1883 \
      --bridge http://127.0.0.1:8801 --window 60 --duration 300
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import threading
import time
import urllib.request
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sensorchain.contracts.anchor import anchor_signing_payload  # noqa: E402
from sensorchain.identity import SoftHSM, key_fingerprint  # noqa: E402
from sensorchain.merkle import MerkleTree  # noqa: E402


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


class EdgeGateway:
    def __init__(self, gateway_id: str, bridge_url: str, window_s: float, store_dir: Path):
        self.gateway_id = gateway_id
        self.bridge_url = bridge_url
        self.window_s = window_s
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.hsm = SoftHSM()
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._batch_counter = 0
        self.anchored = 0
        self.readings_seen = 0
        self.errors = 0

    # ------------------------------------------------------------------
    def register(self) -> dict:
        pem = self.hsm.public_key_pem()
        record = {
            "device_id": self.gateway_id, "sensor_type": "gateway",
            "manufacturer": "RPi-Foundation", "serial_number": f"EDGE-{self.gateway_id}",
            "bis_certificate": f"BIS-17927-{self.gateway_id}", "vendor": "VendorA",
            "city": "pune", "latitude": 18.52, "longitude": 73.85,
            "public_key_pem": pem, "key_fingerprint": key_fingerprint(pem),
        }
        result = post_json(f"{self.bridge_url}/register", record)
        print(f"[edge] registered {self.gateway_id} on-chain "
              f"(fingerprint {result['key_fingerprint']})", flush=True)
        return result

    # ------------------------------------------------------------------
    def on_message(self, _client, _userdata, message) -> None:
        try:
            reading = json.loads(message.payload)
        except json.JSONDecodeError:
            return
        with self._lock:
            self._buffer.append(reading)
            self.readings_seen += 1

    def flush(self) -> dict | None:
        with self._lock:
            if not self._buffer:
                return None
            readings, self._buffer = self._buffer, []
        window_start = float(min(r["timestamp"] for r in readings))
        window_end = float(max(r["timestamp"] for r in readings))
        tree = MerkleTree(readings)
        self._batch_counter += 1
        batch_id = f"{self.gateway_id}-batch-{self._batch_counter:06d}"
        payload = anchor_signing_payload(
            self.gateway_id, batch_id, tree.root, window_start, window_end, len(readings))
        try:
            anchor = post_json(f"{self.bridge_url}/anchor", {
                "gateway_id": self.gateway_id, "batch_id": batch_id,
                "merkle_root": tree.root, "window_start": window_start,
                "window_end": window_end, "count": len(readings),
                "signature": self.hsm.sign(payload),
            })
        except Exception as exc:  # noqa: BLE001 — keep the edge loop alive
            self.errors += 1
            print(f"[edge] anchor FAILED for {batch_id}: {exc}", flush=True)
            return None
        self.anchored += 1
        (self.store_dir / f"{batch_id}.json").write_text(json.dumps({
            "batch_id": batch_id, "merkle_root": tree.root, "readings": readings,
        }))
        print(f"[edge] anchored {batch_id}: {len(readings)} readings "
              f"root={tree.root[:16]}… tx={anchor.get('batch_id', '?')}", flush=True)
        return anchor

    # ------------------------------------------------------------------
    def run(self, broker_host: str, broker_port: int, topic: str, duration_s: float) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"sensorchain-{self.gateway_id}")
        client.on_message = self.on_message
        client.connect(broker_host, broker_port)
        client.subscribe(topic, qos=1)
        client.loop_start()
        print(f"[edge] subscribed to {topic} on {broker_host}:{broker_port}; "
              f"window={self.window_s:.0f}s duration={duration_s:.0f}s", flush=True)

        started = time.time()
        next_flush = started + self.window_s
        try:
            while time.time() - started < duration_s:
                time.sleep(min(0.5, max(0.0, next_flush - time.time())))
                if time.time() >= next_flush:
                    self.flush()
                    next_flush += self.window_s
            self.flush()  # close the final partial window
        finally:
            client.loop_stop()
            client.disconnect()
        self.report_footprint(time.time() - started)

    def report_footprint(self, wall_s: float) -> None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_s = usage.ru_utime + usage.ru_stime
        peak_rss_mb = usage.ru_maxrss / 1024.0  # linux: KiB → MiB
        print(f"\n[edge] ==== run summary ====", flush=True)
        print(f"[edge] readings ingested : {self.readings_seen}", flush=True)
        print(f"[edge] batches anchored  : {self.anchored} ({self.errors} errors)", flush=True)
        print(f"[edge] wall time         : {wall_s:.1f}s", flush=True)
        print(f"[edge] CPU time          : {cpu_s:.2f}s → avg {100.0 * cpu_s / wall_s:.2f}% of one core", flush=True)
        print(f"[edge] peak RSS          : {peak_rss_mb:.1f} MiB", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="localhost:1883")
    parser.add_argument("--bridge", default="http://127.0.0.1:8801")
    parser.add_argument("--topic", default="sensorchain/readings/#")
    parser.add_argument("--gateway-id", default=f"gw-edge-{int(time.time()) % 100000:05d}")
    parser.add_argument("--window", type=float, default=60.0, help="batch window seconds")
    parser.add_argument("--duration", type=float, default=300.0, help="run time seconds")
    parser.add_argument("--store", default="/tmp/sensorchain-batches")
    args = parser.parse_args()

    host, _, port = args.broker.partition(":")
    agent = EdgeGateway(args.gateway_id, args.bridge, args.window,
                        Path(args.store) / args.gateway_id)
    agent.register()
    agent.run(host, int(port or 1883), args.topic, args.duration)


if __name__ == "__main__":
    main()
