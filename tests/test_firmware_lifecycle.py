import hashlib

import pytest

from sensorchain.contracts import AnchorContract, DeviceRegistryContract, FirmwareContract
from sensorchain.contracts.anchor import anchor_signing_payload
from sensorchain.contracts.device_registry import rotation_signing_payload
from sensorchain.contracts.firmware import firmware_signing_payload
from sensorchain.identity import Device, SoftHSM, key_fingerprint
from sensorchain.ledger import Ledger
from sensorchain.merkle import MerkleTree


@pytest.fixture
def ledger():
    return Ledger("pune")


@pytest.fixture
def registry(ledger):
    return DeviceRegistryContract(ledger)


@pytest.fixture
def firmware(ledger):
    return FirmwareContract(ledger)


def register(registry, device_id="aqi-001", **overrides):
    fields = dict(
        device_id=device_id, sensor_type="aqi", manufacturer="Bosch",
        serial_number=f"SN-{device_id}", bis_certificate=f"BIS-{device_id}",
        vendor="VendorA", city="pune", latitude=18.52, longitude=73.85,
    )
    fields.update(overrides)
    device = Device(**fields)
    registry.register_device(device.registration_record())
    return device


FW_IMAGE = b"sensor firmware image v2.1.0 \x7fELF..."
FW_HASH = hashlib.sha256(FW_IMAGE).hexdigest()


def publish(firmware, maker_hsm, model="AQ-Sense-3", version="2.1.0", fw_hash=FW_HASH):
    signature = maker_hsm.sign(firmware_signing_payload("Bosch", model, version, fw_hash))
    return firmware.publish_firmware("Bosch", model, version, fw_hash, signature)


# ----------------------------------------------------------------------
# Firmware publication chain of trust
# ----------------------------------------------------------------------
def test_publish_approve_and_verify_image(ledger, firmware):
    maker = SoftHSM()
    firmware.register_manufacturer("Bosch", maker.public_key_pem())
    publish(firmware, maker)

    # not approved yet → devices must refuse it
    assert firmware.verify_image("AQ-Sense-3", "2.1.0", FW_HASH)["reason"] == "not_approved_for_rollout"

    firmware.approve_firmware("AQ-Sense-3", "2.1.0", approver="PuneSPV")
    assert firmware.verify_image("AQ-Sense-3", "2.1.0", FW_HASH)["ok"]
    assert firmware.latest_approved("AQ-Sense-3")["version"] == "2.1.0"

    # a manipulated image is refused before flashing
    bad = firmware.verify_image("AQ-Sense-3", "2.1.0", hashlib.sha256(b"trojan").hexdigest())
    assert not bad["ok"] and bad["reason"] == "image_hash_mismatch"


def test_wrongly_signed_release_rejected(ledger, firmware):
    maker, imposter = SoftHSM(), SoftHSM()
    firmware.register_manufacturer("Bosch", maker.public_key_pem())
    signature = imposter.sign(firmware_signing_payload("Bosch", "AQ-Sense-3", "9.9.9", FW_HASH))
    with pytest.raises(PermissionError, match="signing key"):
        firmware.publish_firmware("Bosch", "AQ-Sense-3", "9.9.9", FW_HASH, signature)
    assert any(e.name == "FirmwareSignatureInvalid" for e in ledger.events)


def test_report_update_applies_and_flags(ledger, registry, firmware):
    register(registry)
    maker = SoftHSM()
    firmware.register_manufacturer("Bosch", maker.public_key_pem())
    publish(firmware, maker)
    firmware.approve_firmware("AQ-Sense-3", "2.1.0", approver="PuneSPV")

    ok = firmware.report_update("aqi-001", "AQ-Sense-3", "2.1.0", FW_HASH)
    assert ok["ok"]
    assert registry.get_device("aqi-001")["firmware_version"] == "2.1.0"

    # a device reporting a different installed hash gets flagged on-chain
    bad = firmware.report_update("aqi-001", "AQ-Sense-3", "2.1.0",
                                 hashlib.sha256(b"tampered install").hexdigest())
    assert not bad["ok"] and bad["reason"] == "installed_hash_mismatch"
    assert registry.get_device("aqi-001")["status"] == "flagged"
    assert any(e.name == "FirmwareHashMismatch" for e in ledger.events)


def test_unapproved_rollout_flags_device(ledger, registry, firmware):
    register(registry)
    maker = SoftHSM()
    firmware.register_manufacturer("Bosch", maker.public_key_pem())
    publish(firmware, maker)  # published but never approved

    result = firmware.report_update("aqi-001", "AQ-Sense-3", "2.1.0", FW_HASH)
    assert not result["ok"] and result["reason"] == "unapproved_release"
    assert registry.get_device("aqi-001")["status"] == "flagged"


# ----------------------------------------------------------------------
# Key rotation
# ----------------------------------------------------------------------
def test_key_rotation_with_old_key_signature(ledger, registry):
    device = register(registry)
    new_hsm = SoftHSM()
    new_pem = new_hsm.public_key_pem()
    signature = device.hsm.sign(rotation_signing_payload("aqi-001", key_fingerprint(new_pem)))

    updated = registry.rotate_key("aqi-001", new_pem, signature)
    assert updated["key_fingerprint"] == key_fingerprint(new_pem)
    assert len(updated["key_history"]) == 1
    assert updated["key_history"][0]["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----")
    assert any(e.name == "DeviceKeyRotated" for e in ledger.events)


def test_key_rotation_without_old_key_rejected(ledger, registry):
    register(registry)
    thief = SoftHSM()
    new_pem = thief.public_key_pem()
    signature = thief.sign(rotation_signing_payload("aqi-001", key_fingerprint(new_pem)))
    with pytest.raises(PermissionError, match="current device key"):
        registry.rotate_key("aqi-001", new_pem, signature)
    assert any(e.name == "KeyRotationRejected" for e in ledger.events)


def test_anchor_works_with_new_key_only_after_rotation(ledger, registry):
    device = register(registry, device_id="gw-01", sensor_type="gateway")
    anchor_contract = AnchorContract(ledger)
    tree = MerkleTree([{"v": 1}])

    new_hsm = SoftHSM()
    signature = device.hsm.sign(rotation_signing_payload("gw-01", key_fingerprint(new_hsm.public_key_pem())))
    registry.rotate_key("gw-01", new_hsm.public_key_pem(), signature)

    # old key can no longer anchor
    payload = anchor_signing_payload("gw-01", "b1", tree.root, 0.0, 1.0, 1)
    with pytest.raises(PermissionError):
        anchor_contract.anchor_batch("gw-01", "b1", tree.root, 0.0, 1.0, 1, device.hsm.sign(payload))
    # new key can
    anchor = anchor_contract.anchor_batch("gw-01", "b1", tree.root, 0.0, 1.0, 1, new_hsm.sign(payload))
    assert anchor["merkle_root"] == tree.root


# ----------------------------------------------------------------------
# Vendor transfer + decommission
# ----------------------------------------------------------------------
def test_vendor_transfer_keeps_history(ledger, registry):
    register(registry)
    updated = registry.transfer_vendor("aqi-001", "VendorB", authorized_by="PuneSPV")
    assert updated["vendor"] == "VendorB"
    assert updated["vendor_history"][0]["vendor"] == "VendorA"
    assert any(e.name == "DeviceVendorTransferred" for e in ledger.events)


def test_decommission_revokes_anchor_rights(ledger, registry):
    device = register(registry, device_id="gw-01", sensor_type="gateway")
    registry.decommission("gw-01", reason="end of contract")
    record = registry.get_device("gw-01")
    assert record["status"] == "retired" and "decommissioned_at" in record

    tree = MerkleTree([{"v": 1}])
    payload = anchor_signing_payload("gw-01", "b1", tree.root, 0.0, 1.0, 1)
    with pytest.raises(PermissionError, match="retired"):
        AnchorContract(ledger).anchor_batch("gw-01", "b1", tree.root, 0.0, 1.0, 1,
                                            device.hsm.sign(payload))

    with pytest.raises(PermissionError, match="retired"):
        registry.rotate_key("gw-01", SoftHSM().public_key_pem(), "00")
