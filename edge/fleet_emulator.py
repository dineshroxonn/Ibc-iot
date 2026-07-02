#!/usr/bin/env python3
"""Sensor fleet emulator — publishes readings over MQTT.

Emulates a mixed city fleet (AQI / water quality / traffic) publishing
to `sensorchain/readings/<device_id>` at a fixed cadence, standing in
for real sensors until pilot hardware is connected. Reading format is
identical to what real devices will publish (int timestamp,
fixed-decimal string value) so the gateway agent doesn't know the
difference.

Usage:
  python3 edge/fleet_emulator.py --broker localhost:1883 \
      --sensors 60 --cadence 5 --duration 300
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sensorchain.simulator import SimulatedSensor  # noqa: E402

SENSOR_MIX = ["aqi", "aqi", "aqi", "traffic", "water_quality"]  # 60/20/20 mix
UNITS = {"aqi": "AQI", "water_quality": "pH", "traffic": "veh/min"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="localhost:1883")
    parser.add_argument("--sensors", type=int, default=60)
    parser.add_argument("--cadence", type=float, default=5.0, help="seconds between readings per sensor")
    parser.add_argument("--duration", type=float, default=300.0)
    args = parser.parse_args()

    import numpy as np

    fleet = []
    for i in range(args.sensors):
        sensor_type = SENSOR_MIX[i % len(SENSOR_MIX)]
        fleet.append(SimulatedSensor(
            device_id=f"{sensor_type}-{i:04d}", sensor_type=sensor_type,
            rng=np.random.default_rng(4000 + i),
        ))

    host, _, port = args.broker.partition(":")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sensorchain-fleet")
    client.connect(host, int(port or 1883))
    client.loop_start()

    started = time.time()
    published = 0
    print(f"[fleet] {args.sensors} sensors → {host}:{port or 1883} "
          f"every {args.cadence:.0f}s for {args.duration:.0f}s", flush=True)
    try:
        while time.time() - started < args.duration:
            tick = time.time()
            sim_t = tick - started
            for sensor in fleet:
                value = sensor.value_at(sim_t)
                if value is None:
                    continue
                reading = {
                    "device_id": sensor.device_id,
                    "sensor_type": sensor.sensor_type,
                    "timestamp": int(tick),
                    "value": f"{value:.3f}",
                    "unit": UNITS[sensor.sensor_type],
                }
                client.publish(f"sensorchain/readings/{sensor.device_id}",
                               json.dumps(reading), qos=1)
                published += 1
            time.sleep(max(0.0, args.cadence - (time.time() - tick)))
    finally:
        client.loop_stop()
        client.disconnect()
    rate = published / (time.time() - started)
    print(f"[fleet] published {published} readings ({rate:.1f} msg/s)", flush=True)


if __name__ == "__main__":
    main()
