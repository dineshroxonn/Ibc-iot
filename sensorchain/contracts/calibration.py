"""Calibration Certificate Anchor contract.

Certifying labs sign periodic calibration certificates; the signed
certificate hash is anchored on-chain against the device. A device
whose latest certificate has expired (or that never had one) is
auto-flagged, and its subsequent readings carry a degraded-trust
marker in the verification portal.
"""

from __future__ import annotations

import hashlib
import time

from ..ledger import Ledger
from ..merkle import canonical_json


class CalibrationContract:
    CONTRACT = "calibration"

    def __init__(self, ledger: Ledger, validity_seconds: float = 180 * 24 * 3600):
        self.ledger = ledger
        self.validity_seconds = validity_seconds  # default: 180-day calibration cycle

    @staticmethod
    def _key(device_id: str, issued_at: float) -> str:
        return f"calibration~{device_id}~{issued_at:017.6f}"

    def anchor_certificate(
        self,
        device_id: str,
        lab_id: str,
        lab_accreditation: str,
        result: str,
        issued_at: float | None = None,
        parameters: dict | None = None,
    ) -> dict:
        """Anchor a calibration certificate hash for a registered device."""
        if self.ledger.get_state(f"device~{device_id}") is None:
            raise KeyError(f"cannot calibrate unregistered device {device_id}")
        issued_at = issued_at if issued_at is not None else time.time()
        certificate = {
            "device_id": device_id,
            "lab_id": lab_id,
            "lab_accreditation": lab_accreditation,   # e.g. NABL accreditation number
            "result": result,                          # "pass" | "fail"
            "parameters": parameters or {},
            "issued_at": issued_at,
            "expires_at": issued_at + self.validity_seconds,
        }
        certificate["certificate_hash"] = hashlib.sha256(canonical_json(certificate)).hexdigest()
        tx = self.ledger.put_state(
            self.CONTRACT, "anchorCertificate", self._key(device_id, issued_at), certificate
        )
        self.ledger.emit_event("CalibrationAnchored", {
            "device_id": device_id,
            "lab_id": lab_id,
            "result": result,
            "certificate_hash": certificate["certificate_hash"],
            "tx_id": tx.tx_id,
        })
        return certificate

    def latest_certificate(self, device_id: str) -> dict | None:
        certs = [v for _, v in self.ledger.query_by_prefix(f"calibration~{device_id}~")]
        return max(certs, key=lambda c: c["issued_at"]) if certs else None

    def calibration_status(self, device_id: str, now: float | None = None) -> dict:
        """Trust status used by the anchor contract and the portal."""
        now = now if now is not None else time.time()
        cert = self.latest_certificate(device_id)
        if cert is None:
            return {"device_id": device_id, "calibrated": False, "reason": "never_calibrated"}
        if cert["result"] != "pass":
            return {"device_id": device_id, "calibrated": False, "reason": "last_calibration_failed",
                    "certificate_hash": cert["certificate_hash"]}
        if now > cert["expires_at"]:
            return {"device_id": device_id, "calibrated": False, "reason": "calibration_expired",
                    "expired_at": cert["expires_at"], "certificate_hash": cert["certificate_hash"]}
        return {"device_id": device_id, "calibrated": True,
                "expires_at": cert["expires_at"], "certificate_hash": cert["certificate_hash"]}

    def flag_uncalibrated(self, device_ids: list[str], now: float | None = None) -> list[dict]:
        """Sweep a device list; emit UncalibratedDevice events for offenders."""
        flagged = []
        for device_id in device_ids:
            status = self.calibration_status(device_id, now=now)
            if not status["calibrated"]:
                self.ledger.emit_event("UncalibratedDevice", status)
                flagged.append(status)
        return flagged
