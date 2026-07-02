"""Device identity and HSM abstraction.

In production every sensor ships with a keypair generated inside a
hardware security module at manufacture; the private key never leaves
the device. `SoftHSM` reproduces that contract in software for the
prototype: callers can request signatures but can never read the key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)


class SoftHSM:
    """Software stand-in for a per-device HSM (ECDSA P-256).

    The private key is held in a closure-private attribute and is not
    exported by any method — mirroring real HSM semantics where only
    sign() and the public key are available to the host.
    """

    def __init__(self) -> None:
        self.__private_key = ec.generate_private_key(ec.SECP256R1())

    def public_key_pem(self) -> str:
        return (
            self.__private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )

    def sign(self, message: bytes) -> str:
        """Sign a message; returns the DER-encoded ECDSA signature as hex."""
        der = self.__private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return encode_dss_signature(r, s).hex()


def verify_signature(public_key_pem: str, message: bytes, signature_hex: str) -> bool:
    """Verify an ECDSA signature against a device's registered public key."""
    public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    try:
        public_key.verify(bytes.fromhex(signature_hex), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def key_fingerprint(public_key_pem: str) -> str:
    """Short stable identifier for a public key (first 16 hex chars of SHA-256)."""
    return hashlib.sha256(public_key_pem.encode("ascii")).hexdigest()[:16]


@dataclass
class Device:
    """An IoT sensor as registered at procurement time."""

    device_id: str
    sensor_type: str          # e.g. "aqi", "water_quality", "traffic"
    manufacturer: str
    serial_number: str
    bis_certificate: str      # BIS IS 17927 certificate reference
    vendor: str               # system integrator operating the device
    city: str
    latitude: float
    longitude: float
    hsm: SoftHSM = field(default_factory=SoftHSM, repr=False)

    def registration_record(self) -> dict:
        """The on-chain identity record (public data only — no key material)."""
        pem = self.hsm.public_key_pem()
        return {
            "device_id": self.device_id,
            "sensor_type": self.sensor_type,
            "manufacturer": self.manufacturer,
            "serial_number": self.serial_number,
            "bis_certificate": self.bis_certificate,
            "vendor": self.vendor,
            "city": self.city,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "public_key_pem": pem,
            "key_fingerprint": key_fingerprint(pem),
        }
