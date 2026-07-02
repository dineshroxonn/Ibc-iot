"""Device Identity Registry contract.

Every sensor is registered at procurement with its serial number,
manufacturer, BIS IS 17927 certificate reference, GPS location, and the
public half of the HSM keypair generated at manufacture. The record is
immutable; lifecycle status changes (active / maintenance / retired)
are appended as new state versions and emitted as events.
"""

from __future__ import annotations

import time

from ..ledger import Ledger

VALID_STATUSES = {"active", "maintenance", "retired", "flagged"}


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
