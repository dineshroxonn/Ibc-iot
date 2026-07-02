#!/usr/bin/env python3
"""SensorChain live anomaly oracle service.

The AI oracle from the proposal, running against the real network: it
subscribes to the city's MQTT reading stream (raw readings live
off-chain), runs all four detection levels — autoencoder temporal,
sensor silence, spatial divergence, cross-modal physics — and anchors
every finding on-chain via the bridge (Oracle:ReportAnomaly), where it
is stored under the reporting MSP and emitted as an AnomalyDetected
chaincode event.

Per-device autoencoders are self-baselined: each device's model trains
on its own first `--baseline` readings (assumed normal, as at
commissioning time), mirroring the production pattern of quarterly
retraining on CiDaP history.

Usage:
  python3 edge/oracle_service.py --broker localhost:1883 \
      --bridge http://127.0.0.1:8801 --duration 150 --baseline 60
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sensorchain.ledger import Ledger  # noqa: E402
from sensorchain.oracle import AnomalyOracle  # noqa: E402


def http_json(url: str, payload: dict | None = None) -> dict | list:
    if payload is None:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read())
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


class LiveOracle:
    def __init__(self, bridge_url: str, baseline: int, window_s: float):
        self.bridge_url = bridge_url
        self.baseline = baseline
        self.window_s = window_s
        # internal scratch ledger: the oracle's working memory only —
        # authoritative anomaly records go to Fabric via the bridge
        self.oracle = AnomalyOracle(Ledger("oracle-scratch"), window=12,
                                    silence_threshold_s=60.0, spatial_z_threshold=5.0,
                                    dedup_window_s=120.0)
        self._buffer: list[dict] = []
        self._series: dict[str, list[float]] = {}
        self._trained: set[str] = set()
        self._lock = threading.Lock()
        self.reported = 0
        self.report_errors = 0

    def sync_registry(self) -> int:
        devices = http_json(f"{self.bridge_url}/devices")
        self.oracle.load_devices(devices)
        return len(devices)

    # ------------------------------------------------------------------
    def on_message(self, _client, _userdata, message) -> None:
        try:
            reading = json.loads(message.payload)
        except json.JSONDecodeError:
            return
        with self._lock:
            self._buffer.append(reading)

    def _maybe_train(self, device_id: str, value: float) -> None:
        series = self._series.setdefault(device_id, [])
        if device_id in self._trained:
            return
        series.append(value)
        if len(series) >= self.baseline:
            threshold = self.oracle.train(device_id, np.array(series))
            self._trained.add(device_id)
            print(f"[oracle] baselined {device_id}: threshold {threshold:.3f} "
                  f"({len(series)} readings)", flush=True)

    def process_window(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        for reading in batch:
            self._maybe_train(reading["device_id"], float(reading["value"]))
        fresh = self.oracle.observe_batch(batch)
        for anomaly in fresh:
            record = anomaly.to_dict()
            try:
                onchain = http_json(f"{self.bridge_url}/anomaly", record)
                self.reported += 1
                print(f"[oracle] ON-CHAIN [{record['severity'].upper()}] {record['kind']} "
                      f"{record['device_id']}: {record['detail'][:80]} "
                      f"(reported_by={onchain.get('reported_by')})", flush=True)
            except Exception as exc:  # noqa: BLE001 — keep detecting even if reporting hiccups
                self.report_errors += 1
                print(f"[oracle] report FAILED: {exc}", flush=True)

    # ------------------------------------------------------------------
    def run(self, broker_host: str, broker_port: int, topic: str, duration_s: float) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sensorchain-oracle")
        client.on_message = self.on_message
        client.connect(broker_host, broker_port)
        client.subscribe(topic, qos=1)
        client.loop_start()
        device_count = self.sync_registry()
        print(f"[oracle] watching {topic}; {device_count} devices in on-chain registry; "
              f"window={self.window_s:.0f}s duration={duration_s:.0f}s", flush=True)
        started = time.time()
        try:
            while time.time() - started < duration_s:
                time.sleep(self.window_s)
                self.sync_registry()  # pick up devices registered mid-run
                self.process_window()
            self.process_window()
        finally:
            client.loop_stop()
            client.disconnect()
        print(f"\n[oracle] done: {self.reported} anomalies anchored on-chain "
              f"({self.report_errors} report errors)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="localhost:1883")
    parser.add_argument("--bridge", default="http://127.0.0.1:8801")
    parser.add_argument("--topic", default="sensorchain/readings/#")
    parser.add_argument("--window", type=float, default=10.0, help="detection window seconds")
    parser.add_argument("--baseline", type=int, default=60, help="readings per device before the autoencoder trains")
    parser.add_argument("--duration", type=float, default=300.0)
    args = parser.parse_args()

    host, _, port = args.broker.partition(":")
    service = LiveOracle(args.bridge, args.baseline, args.window)
    service.run(host, int(port or 1883), args.topic, args.duration)


if __name__ == "__main__":
    main()
