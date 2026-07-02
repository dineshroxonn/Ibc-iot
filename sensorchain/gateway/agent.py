"""Lightweight Gateway Anchor Agent.

Runs on existing city gateways (Raspberry Pi 4 class). Read-only with
respect to the sensor data path: it observes readings, buffers them
into fixed windows, computes the batch Merkle root, signs the anchor
payload with the gateway HSM, and submits a single small transaction
per window. The full readings stay in the city data platform (modelled
here by the agent's local batch store, which also serves Merkle proofs).

Footprint notes: per window the agent holds only the raw readings plus
hashes — no ML, no history. The production target from the proposal
(<5 MB binary, <2% CPU, <50 MB RAM) follows from this design.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.anchor import AnchorContract, anchor_signing_payload
from ..identity import SoftHSM
from ..merkle import MerkleProof, MerkleTree


@dataclass
class StoredBatch:
    """A closed window retained in the city platform (off-chain)."""

    batch_id: str
    window_start: float
    window_end: float
    readings: list[dict]
    merkle_root: str
    tree: MerkleTree = field(repr=False, default=None)  # type: ignore[assignment]

    def proof_for(self, index: int) -> MerkleProof:
        return self.tree.proof(index)

    def find_reading(self, device_id: str, timestamp: float) -> int | None:
        for i, r in enumerate(self.readings):
            if r["device_id"] == device_id and r["timestamp"] == timestamp:
                return i
        return None


class GatewayAgent:
    def __init__(self, gateway_id: str, hsm: SoftHSM, anchor_contract: AnchorContract,
                 window_seconds: float = 60.0):
        self.gateway_id = gateway_id
        self.hsm = hsm
        self.anchor_contract = anchor_contract
        self.window_seconds = window_seconds
        self._buffer: list[dict] = []
        self._window_start: float | None = None
        self._batch_counter = 0
        self.batches: dict[str, StoredBatch] = {}

    def ingest(self, reading: dict) -> dict | None:
        """Buffer one reading; closes and anchors the window when the
        reading's timestamp crosses the window boundary. Returns the
        anchor record when a window was closed, else None."""
        ts = reading["timestamp"]
        if self._window_start is None:
            self._window_start = ts
        anchor = None
        if ts - self._window_start >= self.window_seconds and self._buffer:
            anchor = self.flush(window_end=self._window_start + self.window_seconds)
            self._window_start = ts
        self._buffer.append(reading)
        return anchor

    def flush(self, window_end: float | None = None) -> dict | None:
        """Close the current window: build the tree, sign, anchor on-chain."""
        if not self._buffer:
            return None
        readings = self._buffer
        self._buffer = []
        window_start = self._window_start if self._window_start is not None else readings[0]["timestamp"]
        window_end = window_end if window_end is not None else readings[-1]["timestamp"]

        tree = MerkleTree(readings)
        self._batch_counter += 1
        batch_id = f"{self.gateway_id}-batch-{self._batch_counter:06d}"

        payload = anchor_signing_payload(
            self.gateway_id, batch_id, tree.root, window_start, window_end, len(readings)
        )
        anchor = self.anchor_contract.anchor_batch(
            gateway_id=self.gateway_id,
            batch_id=batch_id,
            merkle_root=tree.root,
            window_start=window_start,
            window_end=window_end,
            count=len(readings),
            signature_hex=self.hsm.sign(payload),
        )
        self.batches[batch_id] = StoredBatch(
            batch_id=batch_id,
            window_start=window_start,
            window_end=window_end,
            readings=readings,
            merkle_root=tree.root,
            tree=tree,
        )
        return anchor

    # ------------------------------------------------------------------
    # Proof service (what the city platform exposes to the portal)
    # ------------------------------------------------------------------
    def proof_for_reading(self, batch_id: str, index: int) -> dict:
        batch = self.batches[batch_id]
        proof = batch.proof_for(index)
        return {
            "batch_id": batch_id,
            "reading": batch.readings[index],
            "reading_index": index,
            "proof": proof.to_dict(),
            "merkle_root": batch.merkle_root,
        }
