import numpy as np
import pytest

from sensorchain.contracts import DeviceRegistryContract
from sensorchain.identity import Device
from sensorchain.ledger import Ledger
from sensorchain.oracle import AnomalyOracle, Autoencoder
from sensorchain.simulator import SimulatedSensor


def normal_series(n=600, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(n) * 60.0
    return 120.0 + 30.0 * np.sin(2 * np.pi * t / 86_400) + rng.normal(0.0, 6.0, n)


# ----------------------------------------------------------------------
# Autoencoder
# ----------------------------------------------------------------------
def test_autoencoder_accepts_normal_flags_spike():
    model = Autoencoder(window=12)
    series = normal_series()
    model.fit(series)

    normal_window = series[100:112]
    anomalous_window = normal_window.copy()
    anomalous_window[-3:] += 400.0  # step tamper

    assert not model.is_anomalous(normal_window)[0]
    assert model.is_anomalous(anomalous_window)[0]


def test_autoencoder_untrained_raises():
    with pytest.raises(RuntimeError):
        Autoencoder().is_anomalous(np.zeros(12))


# ----------------------------------------------------------------------
# Oracle levels
# ----------------------------------------------------------------------
def build_city(n_aqi=4):
    ledger = Ledger("pune")
    registry = DeviceRegistryContract(ledger)
    for i in range(n_aqi):
        registry.register_device(Device(
            device_id=f"aqi-{i:03d}", sensor_type="aqi", manufacturer="Bosch",
            serial_number=f"SN-{i}", bis_certificate=f"BIS-{i}", vendor="VendorA",
            city="pune", latitude=18.52, longitude=73.85,
        ).registration_record())
    registry.register_device(Device(
        device_id="traffic-000", sensor_type="traffic", manufacturer="Siemens",
        serial_number="SN-T0", bis_certificate="BIS-T0", vendor="VendorB",
        city="pune", latitude=18.52, longitude=73.85,
    ).registration_record())
    oracle = AnomalyOracle(ledger, window=12, silence_threshold_s=300.0)
    oracle.load_registry()
    return ledger, oracle


def readings_at(ts, values):
    return [{"device_id": d, "sensor_type": "aqi" if d.startswith("aqi") else "traffic",
             "timestamp": ts, "value": v} for d, v in values.items()]


def test_temporal_detection_via_oracle():
    ledger, oracle = build_city(n_aqi=1)
    sensor = SimulatedSensor("aqi-000", "aqi")
    oracle.train("aqi-000", sensor.normal_series(600))

    # feed normal readings, then a tampered jump
    for i in range(20):
        oracle.observe_batch(readings_at(1000.0 + 60 * i, {"aqi-000": 120.0 + (i % 3)}))
    assert not [a for a in oracle.anomalies if a.kind == "temporal"]

    for i in range(6):
        oracle.observe_batch(readings_at(2200.0 + 60 * i, {"aqi-000": 600.0}))
    kinds = {a.kind for a in oracle.anomalies}
    assert "temporal" in kinds
    assert any(e.name == "AnomalyDetected" for e in ledger.events)


def test_silence_detection():
    _, oracle = build_city(n_aqi=2)
    oracle.observe_batch(readings_at(1000.0, {"aqi-000": 118.0, "aqi-001": 121.0}))
    # aqi-001 goes dark; aqi-000 keeps reporting 10 minutes later
    oracle.observe_batch(readings_at(1600.0, {"aqi-000": 119.0}))
    silent = [a for a in oracle.anomalies if a.kind == "silence"]
    assert silent and silent[0].device_id == "aqi-001"


def test_spatial_divergence_detection():
    _, oracle = build_city(n_aqi=4)
    oracle.observe_batch(readings_at(1000.0, {
        "aqi-000": 118.0, "aqi-001": 122.0, "aqi-002": 120.0, "aqi-003": 45.0,  # under-reporter
    }))
    spatial = [a for a in oracle.anomalies if a.kind == "spatial"]
    assert spatial and spatial[0].device_id == "aqi-003"


def test_cross_modal_detection():
    _, oracle = build_city(n_aqi=3)
    oracle.observe_batch(readings_at(1000.0, {
        "aqi-000": 260.0, "aqi-001": 255.0, "aqi-002": 262.0,  # severe AQI...
        "traffic-000": 1.0,                                      # ...on an empty road
    }))
    assert [a for a in oracle.anomalies if a.kind == "cross_modal"]
