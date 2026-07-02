"""Pilot city scenario — one Pune shard, end to end.

Builds a small but complete deployment used by both the REST API and
`demo.py`:

  * 1 gateway + 8 sensors (4 AQI + 1 traffic at an arterial-road site,
    3 water-quality at a treatment plant) across 3 vendors
  * device registration with BIS certificates, calibration anchoring
    (one AQI sensor deliberately never calibrated)
  * 30 simulated minutes of readings, batched and Merkle-anchored
    per minute by the gateway agent
  * injected faults: an under-reporting AQI sensor, a traffic counter
    stuck near zero under severe AQI, and a water sensor going dark
  * anomaly oracle trained per device and fed every anchored batch
  * SLA metrics computed from the anchored data and recorded on-chain,
    auto-emitting breach events
  * shard head anchored into the national rollup every 10 minutes
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .contracts import AnchorContract, CalibrationContract, DeviceRegistryContract, SLAContract
from .gateway import GatewayAgent
from .identity import Device
from .ledger import Ledger, NationalRollup
from .oracle import AnomalyOracle
from .simulator import CitySimulator, SimulatedSensor

SIM_START = 1_750_000_000.0
ROAD_SITE = (18.520, 73.850)      # arterial road junction
PLANT_SITE = (18.559, 73.807)     # water treatment plant


@dataclass
class DemoCity:
    ledger: Ledger
    rollup: NationalRollup
    agent: GatewayAgent
    oracle: AnomalyOracle
    devices: list[Device]
    flagged_uncalibrated: list[dict] = field(default_factory=list)
    sla_reports: list[dict] = field(default_factory=list)


def _make_devices() -> tuple[Device, list[Device]]:
    gateway = Device(
        device_id="gw-pune-01", sensor_type="gateway", manufacturer="Cisco",
        serial_number="GW-PN-0001", bis_certificate="BIS-17927-GW-0001",
        vendor="VendorA", city="pune", latitude=ROAD_SITE[0], longitude=ROAD_SITE[1],
    )
    sensors = []
    for i in range(1, 5):
        sensors.append(Device(
            device_id=f"aqi-{i:03d}", sensor_type="aqi", manufacturer="Bosch",
            serial_number=f"SN-AQ-{i:04d}", bis_certificate=f"BIS-17927-AQ-{i:04d}",
            vendor="VendorA", city="pune", latitude=ROAD_SITE[0], longitude=ROAD_SITE[1],
        ))
    sensors.append(Device(
        device_id="traffic-001", sensor_type="traffic", manufacturer="Siemens",
        serial_number="SN-TR-0001", bis_certificate="BIS-17927-TR-0001",
        vendor="VendorB", city="pune", latitude=ROAD_SITE[0], longitude=ROAD_SITE[1],
    ))
    for i in range(1, 4):
        sensors.append(Device(
            device_id=f"water-{i:03d}", sensor_type="water_quality", manufacturer="Xylem",
            serial_number=f"SN-WQ-{i:04d}", bis_certificate=f"BIS-17927-WQ-{i:04d}",
            vendor="VendorC", city="pune", latitude=PLANT_SITE[0], longitude=PLANT_SITE[1],
        ))
    return gateway, sensors


def _make_simulated_fleet(devices: list[Device]) -> list[SimulatedSensor]:
    import numpy as np

    fleet = []
    for n, device in enumerate(devices):
        sim = SimulatedSensor(
            device_id=device.device_id,
            sensor_type=device.sensor_type,
            # the road site sits in a severe-AQI corridor
            site_offset=110.0 if device.sensor_type == "aqi" else 0.0,
            rng=np.random.default_rng(1000 + n),
        )
        fleet.append(sim)

    faults = {s.device_id: s for s in fleet}
    # (a) pollution under-reporting: aqi-003 drops to 40% of true value at t=10min
    faults["aqi-003"].under_report_from = 600.0
    # (b) traffic counter fails to ~0 at t=10min while AQI stays severe → cross-modal
    faults["traffic-001"].under_report_from = 600.0
    faults["traffic-001"].under_report_factor = 0.02
    # (c) water-002 goes dark at t=7min → silence anomaly + VendorC SLA breach
    faults["water-002"].silent_from = 420.0
    return fleet


def build_demo_city(duration_minutes: float = 30.0) -> DemoCity:
    ledger = Ledger("pune")
    rollup = NationalRollup()
    registry = DeviceRegistryContract(ledger)
    calibration = CalibrationContract(ledger)
    anchor_contract = AnchorContract(ledger)
    sla = SLAContract(ledger)

    # 1. registration + calibration + SLA setup ------------------------
    gateway, sensors = _make_devices()
    registry.register_device(gateway.registration_record())
    for device in sensors:
        registry.register_device(device.registration_record())
        if device.device_id != "aqi-004":   # aqi-004 skips calibration on purpose
            calibration.anchor_certificate(
                device.device_id, lab_id="NPL-Delhi", lab_accreditation="NABL-CC-2115",
                result="pass", parameters={"reference": "CPCB-CAAQM"} ,
            )
    flagged = calibration.flag_uncalibrated([d.device_id for d in sensors])

    for vendor in ("VendorA", "VendorB", "VendorC"):
        sla.register_sla(vendor)

    # 2. oracle training on known-normal history -----------------------
    oracle = AnomalyOracle(ledger, window=12, silence_threshold_s=300.0)
    oracle.load_registry()
    fleet = _make_simulated_fleet(sensors)
    for sim in fleet:
        # train on history at the live reporting cadence so the learned
        # dynamics match what the oracle will observe
        oracle.train(sim.device_id, sim.normal_series(600, step=12.0))

    # 3. live run: simulate → batch → anchor → detect -------------------
    agent = GatewayAgent("gw-pune-01", gateway.hsm, anchor_contract, window_seconds=60.0)
    simulator = CitySimulator(fleet, start_time=SIM_START, interval_s=12.0)
    rollup_every = 10 * 60.0
    next_rollup = SIM_START + rollup_every

    def on_anchored(anchor: dict) -> None:
        nonlocal next_rollup
        batch = agent.batches[anchor["batch_id"]]
        oracle.observe_batch(batch.readings)
        if batch.window_end >= next_rollup:
            rollup.anchor_shard(ledger)
            next_rollup += rollup_every

    for reading in simulator.run(duration_minutes * 60.0):
        anchor = agent.ingest(reading)
        if anchor:
            on_anchored(anchor)
    final = agent.flush()
    if final:
        on_anchored(final)

    # 4. SLA evaluation off the anchored data, then close the period by
    #    anchoring the shard head into the national rollup
    sla_reports = _evaluate_slas(sla, calibration, sensors, agent,
                                 duration_minutes, interval_s=12.0)
    rollup.anchor_shard(ledger)

    return DemoCity(
        ledger=ledger, rollup=rollup, agent=agent, oracle=oracle,
        devices=[gateway, *sensors], flagged_uncalibrated=flagged,
        sla_reports=sla_reports,
    )


def _evaluate_slas(sla: SLAContract, calibration: CalibrationContract,
                   sensors: list[Device], agent: GatewayAgent,
                   duration_minutes: float, interval_s: float) -> list[dict]:
    """Compute per-vendor metrics from the anchored batches (auditable:
    every counted reading is Merkle-provable against on-chain roots)."""
    expected_per_device = int(duration_minutes * 60.0 // interval_s)
    counts: dict[str, int] = defaultdict(int)
    last_seen: dict[str, float] = {}
    max_gap: dict[str, float] = defaultdict(float)
    run_end = SIM_START + duration_minutes * 60.0

    for batch in agent.batches.values():
        for reading in batch.readings:
            device_id = reading["device_id"]
            counts[device_id] += 1
            if device_id in last_seen:
                max_gap[device_id] = max(max_gap[device_id],
                                         reading["timestamp"] - last_seen[device_id])
            last_seen[device_id] = reading["timestamp"]
    for device in sensors:
        tail = run_end - last_seen.get(device.device_id, SIM_START)
        max_gap[device.device_id] = max(max_gap[device.device_id], tail)

    by_vendor: dict[str, list[Device]] = defaultdict(list)
    for device in sensors:
        by_vendor[device.vendor].append(device)

    reports = []
    for vendor, fleet in sorted(by_vendor.items()):
        total_expected = expected_per_device * len(fleet)
        total_actual = sum(counts[d.device_id] for d in fleet)
        uncalibrated = [d for d in fleet if not calibration.calibration_status(d.device_id)["calibrated"]]
        metrics = {
            "completeness_pct": round(100.0 * total_actual / total_expected, 2),
            "max_silence_minutes": round(max(max_gap[d.device_id] for d in fleet) / 60.0, 2),
            "uncalibrated_pct": round(100.0 * len(uncalibrated) / len(fleet), 2),
        }
        reports.append(sla.record_metrics(vendor, period="pilot-window-01", metrics=metrics))
    return reports
