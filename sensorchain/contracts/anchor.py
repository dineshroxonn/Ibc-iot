"""Merkle-Root Batch Anchor contract.

The gateway agent submits one transaction per batch window containing
the Merkle root of that window's readings, signed by the gateway's HSM
key. The contract verifies the signature against the registered public
key before accepting the anchor — an anchor from an unregistered or
retired gateway is rejected, and a bad signature is rejected and
reported as a tamper event.
"""

from __future__ import annotations

import time

from ..identity import verify_signature
from ..ledger import Ledger
from ..merkle import MerkleProof


def anchor_signing_payload(gateway_id: str, batch_id: str, merkle_root: str,
                           window_start: float, window_end: float, count: int) -> bytes:
    """The exact bytes a gateway signs when anchoring a batch."""
    return f"{gateway_id}|{batch_id}|{merkle_root}|{window_start:.3f}|{window_end:.3f}|{count}".encode("utf-8")


class AnchorContract:
    CONTRACT = "anchor"

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def _key(batch_id: str) -> str:
        return f"anchor~{batch_id}"

    def anchor_batch(
        self,
        gateway_id: str,
        batch_id: str,
        merkle_root: str,
        window_start: float,
        window_end: float,
        count: int,
        signature_hex: str,
    ) -> dict:
        gateway = self.ledger.get_state(f"device~{gateway_id}")
        if gateway is None:
            raise PermissionError(f"anchor rejected: gateway {gateway_id} is not registered")
        if gateway.get("status") == "retired":
            raise PermissionError(f"anchor rejected: gateway {gateway_id} is retired")

        payload = anchor_signing_payload(gateway_id, batch_id, merkle_root, window_start, window_end, count)
        if not verify_signature(gateway["public_key_pem"], payload, signature_hex):
            self.ledger.emit_event("AnchorSignatureInvalid", {
                "gateway_id": gateway_id, "batch_id": batch_id, "merkle_root": merkle_root,
            })
            raise PermissionError(f"anchor rejected: invalid signature from {gateway_id}")

        if self.ledger.get_state(self._key(batch_id)) is not None:
            raise ValueError(f"batch {batch_id} already anchored")

        anchor = {
            "batch_id": batch_id,
            "gateway_id": gateway_id,
            "merkle_root": merkle_root,
            "window_start": window_start,
            "window_end": window_end,
            "reading_count": count,
            "signature": signature_hex,
            "anchored_at": time.time(),
        }
        tx = self.ledger.put_state(self.CONTRACT, "anchorBatch", self._key(batch_id), anchor)
        anchor["tx_id"] = tx.tx_id
        return anchor

    def get_anchor(self, batch_id: str) -> dict | None:
        return self.ledger.get_state(self._key(batch_id))

    def list_anchors(self, gateway_id: str | None = None) -> list[dict]:
        anchors = [v for _, v in self.ledger.query_by_prefix("anchor~")]
        if gateway_id is not None:
            anchors = [a for a in anchors if a["gateway_id"] == gateway_id]
        return sorted(anchors, key=lambda a: a["window_start"])

    def verify_reading(self, batch_id: str, proof: MerkleProof) -> dict:
        """Citizen-facing verification: does this proof chain up to the
        on-chain root for the claimed batch?"""
        anchor = self.get_anchor(batch_id)
        if anchor is None:
            return {"verified": False, "reason": f"no anchor on-chain for batch {batch_id}"}
        if proof.verify(anchor["merkle_root"]):
            return {"verified": True, "batch_id": batch_id,
                    "merkle_root": anchor["merkle_root"], "anchored_at": anchor["anchored_at"]}
        return {"verified": False, "reason": "Merkle proof does not match on-chain root — data altered after anchoring",
                "expected_root": anchor["merkle_root"], "computed_root": proof.compute_root()}
