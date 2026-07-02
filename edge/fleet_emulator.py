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


# co-location sites for the on-chain registry (drives spatial + cross-modal detection)
SITES = [(18.520, 73.850), (18.559, 73.807), (18.505, 73.926)]


def register_fleet(fleet: list, bridge_url: str, run_tag: str) -> None:
    """Register every emulated sensor on-chain (BIS cert + keypair) and
    anchor a calibration certificate — except the last sensor, which is
    deliberately left uncalibrated so the portal shows the auto-flag."""
    import json as _json
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    from sensorchain.identity import SoftHSM, key_fingerprint

    def post(path: str, payload: dict) -> None:
        request = urllib.request.Request(
            f"{bridge_url}{path}", data=_json.dumps(payload).encode(),
            headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=60).read()

    def register(item):
        index, sensor = item
        site = SITES[index % len(SITES)]
        pem = SoftHSM().public_key_pem()
        post("/register", {
            "device_id": sensor.device_id, "sensor_type": sensor.sensor_type,
            "manufacturer": "Bosch", "serial_number": f"SN-{run_tag}-{index:04d}",
            "bis_certificate": f"BIS-17927-{run_tag}-{index:04d}", "vendor": "VendorA",
            "city": "pune", "latitude": site[0], "longitude": site[1],
            "public_key_pem": pem, "key_fingerprint": key_fingerprint(pem),
        })
        if index != len(fleet) - 1:   # last sensor stays uncalibrated on purpose
            post("/calibrate", {
                "device_id": sensor.device_id, "lab_id": "NPL-Delhi",
                "lab_accreditation": "NABL-CC-2115", "result": "pass",
                "parameters": {"reference": "CPCB-CAAQM"},
            })

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(register, enumerate(fleet)))
    print(f"[fleet] registered {len(fleet)} sensors on-chain "
          f"({len(fleet) - 1} calibrated, 1 left uncalibrated)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="localhost:1883")
    parser.add_argument("--sensors", type=int, default=60)
    parser.add_argument("--cadence", type=float, default=5.0, help="seconds between readings per sensor")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--register", action="store_true",
                        help="register the fleet on-chain via the bridge before publishing")
    parser.add_argument("--bridge", default="http://127.0.0.1:8801")
    parser.add_argument("--inject", action="store_true",
                        help="inject live faults: an under-reporting AQI sensor and a sensor going dark")
    args = parser.parse_args()

    import numpy as np

    fleet = []
    for i in range(args.sensors):
        sensor_type = SENSOR_MIX[i % len(SENSOR_MIX)]
        fleet.append(SimulatedSensor(
            device_id=f"{sensor_type}-{i:04d}", sensor_type=sensor_type,
            rng=np.random.default_rng(4000 + i),
        ))

    if args.inject:
        # fault (a): an AQI sensor under-reports to 40% partway through the
        # run — index 1 sits at a site with three co-located AQI sensors,
        # so both the temporal and spatial detectors can catch it
        fleet[1].under_report_from = args.duration * 0.55
        # fault (b): another sensor goes completely dark
        fleet[2].silent_from = args.duration * 0.5
        print(f"[fleet] faults armed: {fleet[1].device_id} under-reports from "
              f"t={fleet[1].under_report_from:.0f}s; {fleet[2].device_id} silent from "
              f"t={fleet[2].silent_from:.0f}s", flush=True)

    if args.register:
        register_fleet(fleet, args.bridge, run_tag=str(int(time.time()) % 100000))

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
