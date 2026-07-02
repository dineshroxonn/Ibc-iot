import time

import pytest

from sensorchain.contracts import (
    AnchorContract,
    CalibrationContract,
    DeviceRegistryContract,
    SLAContract,
)
from sensorchain.contracts.anchor import anchor_signing_payload
from sensorchain.identity import Device, SoftHSM
from sensorchain.ledger import Ledger, NationalRollup
from sensorchain.merkle import MerkleTree


@pytest.fixture
def ledger():
    return Ledger("pune")


@pytest.fixture
def registry(ledger):
    return DeviceRegistryContract(ledger)


def make_device(device_id="aqi-001", **overrides):
    fields = dict(
        device_id=device_id, sensor_type="aqi", manufacturer="Bosch",
        serial_number=f"SN-{device_id}", bis_certificate=f"BIS-17927-{device_id}",
        vendor="VendorA", city="pune", latitude=18.52, longitude=73.85,
    )
    fields.update(overrides)
    return Device(**fields)


# ----------------------------------------------------------------------
# Device registry
# ----------------------------------------------------------------------
def test_register_and_fetch_device(registry):
    device = make_device()
    registry.register_device(device.registration_record())
    stored = registry.get_device("aqi-001")
    assert stored["status"] == "active"
    assert stored["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----")


def test_duplicate_registration_rejected(registry):
    record = make_device().registration_record()
    registry.register_device(record)
    with pytest.raises(ValueError, match="already registered"):
        registry.register_device(record)


def test_missing_bis_certificate_rejected(registry):
    record = make_device(bis_certificate="").registration_record()
    with pytest.raises(ValueError, match="BIS"):
        registry.register_device(record)


def test_status_lifecycle(registry):
    registry.register_device(make_device().registration_record())
    updated = registry.set_status("aqi-001", "maintenance", reason="scheduled")
    assert updated["status"] == "maintenance"
    with pytest.raises(ValueError):
        registry.set_status("aqi-001", "exploded")


# ----------------------------------------------------------------------
# Calibration
# ----------------------------------------------------------------------
def test_calibration_lifecycle(ledger, registry):
    registry.register_device(make_device().registration_record())
    calibration = CalibrationContract(ledger, validity_seconds=3600)

    status = calibration.calibration_status("aqi-001")
    assert not status["calibrated"] and status["reason"] == "never_calibrated"

    calibration.anchor_certificate("aqi-001", lab_id="NPL-Delhi", lab_accreditation="NABL-123", result="pass")
    assert calibration.calibration_status("aqi-001")["calibrated"]

    # expired certificate flips the device back to uncalibrated
    future = time.time() + 7200
    status = calibration.calibration_status("aqi-001", now=future)
    assert not status["calibrated"] and status["reason"] == "calibration_expired"


def test_flag_uncalibrated_emits_events(ledger, registry):
    registry.register_device(make_device("aqi-001").registration_record())
    registry.register_device(make_device("aqi-002").registration_record())
    calibration = CalibrationContract(ledger)
    calibration.anchor_certificate("aqi-001", "NPL-Delhi", "NABL-123", "pass")

    flagged = calibration.flag_uncalibrated(["aqi-001", "aqi-002"])
    assert [f["device_id"] for f in flagged] == ["aqi-002"]
    assert any(e.name == "UncalibratedDevice" for e in ledger.events)


def test_calibrating_unregistered_device_rejected(ledger):
    with pytest.raises(KeyError):
        CalibrationContract(ledger).anchor_certificate("ghost", "lab", "acc", "pass")


# ----------------------------------------------------------------------
# Anchoring
# ----------------------------------------------------------------------
def anchor_one_batch(ledger, registry, device):
    registry.register_device(device.registration_record())
    anchor_contract = AnchorContract(ledger)
    readings = [{"device_id": device.device_id, "timestamp": 1000.0 + i, "value": 100.0 + i}
                for i in range(5)]
    tree = MerkleTree(readings)
    payload = anchor_signing_payload(device.device_id, "batch-1", tree.root, 1000.0, 1004.0, 5)
    anchor = anchor_contract.anchor_batch(
        device.device_id, "batch-1", tree.root, 1000.0, 1004.0, 5, device.hsm.sign(payload))
    return anchor_contract, tree, readings, anchor


def test_anchor_and_verify(ledger, registry):
    device = make_device("gw-001", sensor_type="gateway")
    anchor_contract, tree, readings, anchor = anchor_one_batch(ledger, registry, device)
    assert anchor["merkle_root"] == tree.root

    result = anchor_contract.verify_reading("batch-1", tree.proof(2))
    assert result["verified"]


def test_anchor_with_wrong_key_rejected(ledger, registry):
    device = make_device("gw-001", sensor_type="gateway")
    registry.register_device(device.registration_record())
    imposter = SoftHSM()
    tree = MerkleTree([{"v": 1}])
    payload = anchor_signing_payload("gw-001", "batch-x", tree.root, 0.0, 1.0, 1)
    with pytest.raises(PermissionError, match="invalid signature"):
        AnchorContract(ledger).anchor_batch("gw-001", "batch-x", tree.root, 0.0, 1.0, 1,
                                            imposter.sign(payload))
    assert any(e.name == "AnchorSignatureInvalid" for e in ledger.events)


def test_anchor_from_unregistered_gateway_rejected(ledger):
    hsm = SoftHSM()
    tree = MerkleTree([{"v": 1}])
    payload = anchor_signing_payload("ghost", "batch-x", tree.root, 0.0, 1.0, 1)
    with pytest.raises(PermissionError, match="not registered"):
        AnchorContract(ledger).anchor_batch("ghost", "batch-x", tree.root, 0.0, 1.0, 1, hsm.sign(payload))


def test_duplicate_batch_rejected(ledger, registry):
    device = make_device("gw-001", sensor_type="gateway")
    anchor_contract, tree, _, _ = anchor_one_batch(ledger, registry, device)
    payload = anchor_signing_payload("gw-001", "batch-1", tree.root, 1000.0, 1004.0, 5)
    with pytest.raises(ValueError, match="already anchored"):
        anchor_contract.anchor_batch("gw-001", "batch-1", tree.root, 1000.0, 1004.0, 5,
                                     device.hsm.sign(payload))


# ----------------------------------------------------------------------
# SLA
# ----------------------------------------------------------------------
def test_sla_breach_auto_event(ledger):
    sla = SLAContract(ledger)
    sla.register_sla("VendorA", {"min_completeness_pct": 95.0})

    ok = sla.record_metrics("VendorA", "2026-06", {"completeness_pct": 99.1, "max_silence_minutes": 2.0})
    assert not ok["breached"]

    bad = sla.record_metrics("VendorA", "2026-07",
                             {"completeness_pct": 80.0, "max_silence_minutes": 45.0})
    assert bad["breached"] and len(bad["violations"]) == 2
    assert bad["penalty_inr"] == 100_000
    breach_events = [e for e in ledger.events if e.name == "SLABreach"]
    assert len(breach_events) == 1
    assert sla.breaches("VendorA")[0]["period"] == "2026-07"


def test_sla_unregistered_vendor(ledger):
    with pytest.raises(KeyError):
        SLAContract(ledger).record_metrics("Nobody", "2026-07", {})


# ----------------------------------------------------------------------
# Ledger integrity + rollup
# ----------------------------------------------------------------------
def test_chain_validates_and_detects_tampering(ledger, registry):
    registry.register_device(make_device().registration_record())
    ok, _ = ledger.validate_chain()
    assert ok

    ledger.blocks[0].transactions[0].value["device_id"] = "forged"
    ok, reason = ledger.validate_chain()
    assert not ok and "tampered" in reason


def test_national_rollup_anchors_shard_heads(registry, ledger):
    registry.register_device(make_device().registration_record())
    rollup = NationalRollup()
    rollup.anchor_shard(ledger)
    registry.register_device(make_device("aqi-002").registration_record())
    rollup.anchor_shard(ledger)

    anchors = rollup.shard_anchors("pune")
    assert len(anchors) == 2
    assert anchors[-1]["head_hash"] == ledger.head_hash()
    assert rollup.ledger.validate_chain()[0]
