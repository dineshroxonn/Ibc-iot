"""Secure Firmware Upgrade contract.

Closes the "secure upgrade mechanisms" requirement of the IoT/IIoT use
case. The trust chain:

  1. A manufacturer is registered on-chain with its firmware-signing
     public key.
  2. Each firmware release (model, version, SHA-256 of the image) is
     published with the manufacturer's signature; the contract verifies
     it before anchoring. Unsigned or wrongly-signed releases are
     rejected.
  3. The city SPV approves a published release for rollout. Devices
     only ever apply approved firmware.
  4. Before flashing, a device (or its gateway) fetches the on-chain
     record and checks the image hash against it. After flashing it
     reports the installed hash; a mismatch auto-flags the device and
     emits a FirmwareHashMismatch event — a tampered or trojaned image
     is caught at rollout time, on an auditable channel.
"""

from __future__ import annotations

import time

from ..identity import key_fingerprint, verify_signature
from ..ledger import Ledger


def firmware_signing_payload(manufacturer: str, model: str, version: str, firmware_hash: str) -> bytes:
    """The exact bytes a manufacturer signs when releasing firmware."""
    return f"firmware|{manufacturer}|{model}|{version}|{firmware_hash}".encode("utf-8")


class FirmwareContract:
    CONTRACT = "firmware"

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    # ------------------------------------------------------------------
    # Manufacturer signing keys
    # ------------------------------------------------------------------
    def register_manufacturer(self, name: str, public_key_pem: str) -> dict:
        key = f"manufacturer~{name}"
        if self.ledger.get_state(key) is not None:
            raise ValueError(f"manufacturer {name} already registered")
        record = {
            "name": name,
            "public_key_pem": public_key_pem,
            "key_fingerprint": key_fingerprint(public_key_pem),
            "registered_at": time.time(),
        }
        self.ledger.put_state(self.CONTRACT, "registerManufacturer", key, record)
        self.ledger.emit_event("ManufacturerRegistered", {
            "name": name, "key_fingerprint": record["key_fingerprint"],
        })
        return record

    # ------------------------------------------------------------------
    # Release lifecycle: publish (manufacturer) → approve (city SPV)
    # ------------------------------------------------------------------
    @staticmethod
    def _release_key(model: str, version: str) -> str:
        return f"firmware~{model}~{version}"

    def publish_firmware(self, manufacturer: str, model: str, version: str,
                         firmware_hash: str, signature_hex: str) -> dict:
        maker = self.ledger.get_state(f"manufacturer~{manufacturer}")
        if maker is None:
            raise KeyError(f"unknown manufacturer {manufacturer}")
        payload = firmware_signing_payload(manufacturer, model, version, firmware_hash)
        if not verify_signature(maker["public_key_pem"], payload, signature_hex):
            self.ledger.emit_event("FirmwareSignatureInvalid", {
                "manufacturer": manufacturer, "model": model, "version": version,
            })
            raise PermissionError(
                f"firmware {model} {version} rejected: signature does not match "
                f"{manufacturer}'s registered signing key")
        key = self._release_key(model, version)
        if self.ledger.get_state(key) is not None:
            raise ValueError(f"firmware {model} {version} already published")
        release = {
            "manufacturer": manufacturer,
            "model": model,
            "version": version,
            "firmware_hash": firmware_hash,
            "signature": signature_hex,
            "approved": False,
            "published_at": time.time(),
        }
        self.ledger.put_state(self.CONTRACT, "publishFirmware", key, release)
        self.ledger.emit_event("FirmwarePublished", {
            "manufacturer": manufacturer, "model": model, "version": version,
            "firmware_hash": firmware_hash,
        })
        return release

    def approve_firmware(self, model: str, version: str, approver: str) -> dict:
        release = self.ledger.get_state(self._release_key(model, version))
        if release is None:
            raise KeyError(f"no published firmware {model} {version}")
        updated = dict(release, approved=True, approved_by=approver, approved_at=time.time())
        self.ledger.put_state(self.CONTRACT, "approveFirmware", self._release_key(model, version), updated)
        self.ledger.emit_event("FirmwareApproved", {
            "model": model, "version": version, "approved_by": approver,
        })
        return updated

    def get_release(self, model: str, version: str) -> dict | None:
        return self.ledger.get_state(self._release_key(model, version))

    def latest_approved(self, model: str) -> dict | None:
        releases = [v for _, v in self.ledger.query_by_prefix(f"firmware~{model}~") if v["approved"]]
        return max(releases, key=lambda r: r["published_at"]) if releases else None

    # ------------------------------------------------------------------
    # Device side: verify before flashing, report after
    # ------------------------------------------------------------------
    def verify_image(self, model: str, version: str, image_hash: str) -> dict:
        """What a device/gateway calls BEFORE flashing an image it received."""
        release = self.get_release(model, version)
        if release is None:
            return {"ok": False, "reason": "unknown_release"}
        if not release["approved"]:
            return {"ok": False, "reason": "not_approved_for_rollout"}
        if release["firmware_hash"] != image_hash:
            return {"ok": False, "reason": "image_hash_mismatch",
                    "expected": release["firmware_hash"], "got": image_hash}
        return {"ok": True, "release": release}

    def report_update(self, device_id: str, model: str, version: str, installed_hash: str) -> dict:
        """Post-flash attestation. A hash mismatch flags the device on-chain."""
        device_key = f"device~{device_id}"
        device = self.ledger.get_state(device_key)
        if device is None:
            raise KeyError(f"unknown device {device_id}")
        release = self.get_release(model, version)
        if release is None:
            raise KeyError(f"no published firmware {model} {version}")

        if release["approved"] and installed_hash == release["firmware_hash"]:
            updated = dict(device, firmware_model=model, firmware_version=version,
                           firmware_updated_at=time.time())
            self.ledger.put_state(self.CONTRACT, "reportUpdate", device_key, updated)
            self.ledger.emit_event("FirmwareUpdateApplied", {
                "device_id": device_id, "model": model, "version": version,
            })
            return {"ok": True, "device": updated}

        reason = "unapproved_release" if not release["approved"] else "installed_hash_mismatch"
        flagged = dict(device, status="flagged",
                       status_reason=f"firmware violation: {reason}",
                       status_updated_at=time.time())
        self.ledger.put_state(self.CONTRACT, "reportUpdate", device_key, flagged)
        self.ledger.emit_event("FirmwareHashMismatch", {
            "device_id": device_id, "model": model, "version": version,
            "reason": reason,
            "expected": release["firmware_hash"], "got": installed_hash,
        })
        return {"ok": False, "reason": reason, "device": flagged}
