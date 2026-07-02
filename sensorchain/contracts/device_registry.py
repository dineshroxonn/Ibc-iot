"""Device Identity Registry contract.

Every sensor is registered at procurement with its serial number,
manufacturer, BIS IS 17927 certificate reference, GPS location, and the
public half of the HSM keypair generated at manufacture. The record is
immutable; lifecycle status changes (active / maintenance / retired)
are appended as new state versions and emitted as events.
"""

from __future__ import annotations

import time

from ..identity import key_fingerprint, verify_signature
from ..ledger import Ledger

VALID_STATUSES = {"active", "maintenance", "retired", "flagged"}


def rotation_signing_payload(device_id: str, new_key_fingerprint: str) -> bytes:
    """Bytes signed by the OLD device key to authorize a key rotation."""
    return f"rotate|{device_id}|{new_key_fingerprint}".encode("utf-8")


class DeviceRegistryContract:
    CONTRACT = "device_registry"

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def _key(device_id: str) -> str:
        return f"device~{device_id}"

    def register_device(self, record: dict) -> dict:
        """Anchor a device identity record. Rejects duplicates and records
        missing BIS certification (a procurement-compliance requirement)."""
        required = {
            "device_id", "sensor_type", "manufacturer", "serial_number",
            "bis_certificate", "vendor", "city", "latitude", "longitude",
            "public_key_pem", "key_fingerprint",
        }
        missing = required - record.keys()
        if missing:
            raise ValueError(f"device record missing fields: {sorted(missing)}")
        key = self._key(record["device_id"])
        if self.ledger.get_state(key) is not None:
            raise ValueError(f"device {record['device_id']} already registered")
        if not record["bis_certificate"]:
            raise ValueError(f"device {record['device_id']} has no BIS IS 17927 certificate — registration refused")

        stored = dict(record, status="active", registered_at=time.time())
        tx = self.ledger.put_state(self.CONTRACT, "registerDevice", key, stored)
        self.ledger.emit_event("DeviceRegistered", {
            "device_id": record["device_id"],
            "vendor": record["vendor"],
            "key_fingerprint": record["key_fingerprint"],
            "tx_id": tx.tx_id,
        })
        return stored

    def get_device(self, device_id: str) -> dict | None:
        return self.ledger.get_state(self._key(device_id))

    def set_status(self, device_id: str, status: str, reason: str = "") -> dict:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}")
        record = self.get_device(device_id)
        if record is None:
            raise KeyError(f"unknown device {device_id}")
        updated = dict(record, status=status, status_reason=reason, status_updated_at=time.time())
        self.ledger.put_state(self.CONTRACT, "setStatus", self._key(device_id), updated)
        self.ledger.emit_event("DeviceStatusChanged", {
            "device_id": device_id, "status": status, "reason": reason,
        })
        return updated

    def list_devices(self, vendor: str | None = None) -> list[dict]:
        devices = [v for _, v in self.ledger.query_by_prefix("device~")]
        if vendor is not None:
            devices = [d for d in devices if d["vendor"] == vendor]
        return devices

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------
    def rotate_key(self, device_id: str, new_public_key_pem: str, signature_hex: str) -> dict:
        """Rotate the device key. Authorized by a signature from the OLD
        key (proof of possession), so a stolen device identity cannot be
        re-keyed by whoever holds only the serial number. The old key is
        preserved in on-chain history so past anchors stay attributable."""
        record = self.get_device(device_id)
        if record is None:
            raise KeyError(f"unknown device {device_id}")
        if record["status"] == "retired":
            raise PermissionError(f"cannot rotate key of retired device {device_id}")
        new_fingerprint = key_fingerprint(new_public_key_pem)
        payload = rotation_signing_payload(device_id, new_fingerprint)
        if not verify_signature(record["public_key_pem"], payload, signature_hex):
            self.ledger.emit_event("KeyRotationRejected", {
                "device_id": device_id, "attempted_fingerprint": new_fingerprint,
            })
            raise PermissionError(
                f"key rotation for {device_id} rejected: not signed by the current device key")
        history = record.get("key_history", [])
        history.append({
            "key_fingerprint": record["key_fingerprint"],
            "public_key_pem": record["public_key_pem"],
            "retired_at": time.time(),
        })
        updated = dict(record, public_key_pem=new_public_key_pem,
                       key_fingerprint=new_fingerprint, key_history=history)
        self.ledger.put_state(self.CONTRACT, "rotateKey", self._key(device_id), updated)
        self.ledger.emit_event("DeviceKeyRotated", {
            "device_id": device_id,
            "old_fingerprint": history[-1]["key_fingerprint"],
            "new_fingerprint": new_fingerprint,
        })
        return updated

    def transfer_vendor(self, device_id: str, new_vendor: str, authorized_by: str) -> dict:
        """Move a device between system integrators (contract re-award,
        O&M handover). Vendor history stays on-chain so responsibility
        for any past reading remains attributable to the vendor of record
        at that time."""
        record = self.get_device(device_id)
        if record is None:
            raise KeyError(f"unknown device {device_id}")
        history = record.get("vendor_history", [])
        history.append({"vendor": record["vendor"], "until": time.time()})
        updated = dict(record, vendor=new_vendor, vendor_history=history)
        self.ledger.put_state(self.CONTRACT, "transferVendor", self._key(device_id), updated)
        self.ledger.emit_event("DeviceVendorTransferred", {
            "device_id": device_id, "from_vendor": history[-1]["vendor"],
            "to_vendor": new_vendor, "authorized_by": authorized_by,
        })
        return updated

    def decommission(self, device_id: str, reason: str) -> dict:
        """End of life: sets the device to retired, which cryptographically
        revokes its anchor rights (the anchor contract refuses retired
        devices). Irreversible."""
        record = self.get_device(device_id)
        if record is None:
            raise KeyError(f"unknown device {device_id}")
        updated = dict(record, status="retired", status_reason=reason,
                       decommissioned_at=time.time())
        self.ledger.put_state(self.CONTRACT, "decommission", self._key(device_id), updated)
        self.ledger.emit_event("DeviceDecommissioned", {
            "device_id": device_id, "reason": reason,
        })
        return updated
