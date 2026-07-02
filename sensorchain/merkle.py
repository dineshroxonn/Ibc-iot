"""Merkle tree batch anchoring.

Sensor readings are hashed into a Merkle tree once per batch window
(1 minute in production). Only the 32-byte root goes on-chain; any
individual reading is later verifiable against that root with an
O(log n) inclusion proof. Raw data never leaves the city platform.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding so the same reading always hashes identically."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def hash_leaf(reading: dict) -> str:
    """Leaf hash: domain-separated SHA-256 of the canonical reading."""
    return hashlib.sha256(b"\x00" + canonical_json(reading)).hexdigest()


def hash_pair(left: str, right: str) -> str:
    """Internal-node hash: domain-separated SHA-256 of concatenated child hashes."""
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


@dataclass
class MerkleProof:
    """Inclusion proof for one leaf: sibling hashes bottom-up with their side."""

    leaf_hash: str
    # each step: (sibling_hash, "left"|"right") — the side the sibling sits on
    path: list[tuple[str, str]] = field(default_factory=list)

    def compute_root(self) -> str:
        node = self.leaf_hash
        for sibling, side in self.path:
            node = hash_pair(sibling, node) if side == "left" else hash_pair(node, sibling)
        return node

    def verify(self, expected_root: str) -> bool:
        return self.compute_root() == expected_root

    def to_dict(self) -> dict:
        return {"leaf_hash": self.leaf_hash, "path": [[h, s] for h, s in self.path]}

    @classmethod
    def from_dict(cls, data: dict) -> "MerkleProof":
        return cls(leaf_hash=data["leaf_hash"], path=[(h, s) for h, s in data["path"]])


class MerkleTree:
    """Binary Merkle tree over a batch of sensor readings.

    Odd nodes at any level are promoted by pairing with a duplicate of
    themselves, which keeps proof generation simple and deterministic.
    """

    def __init__(self, readings: list[dict]):
        if not readings:
            raise ValueError("cannot build a Merkle tree over an empty batch")
        self.leaves = [hash_leaf(r) for r in readings]
        self.levels: list[list[str]] = [self.leaves]
        level = self.leaves
        while len(level) > 1:
            if len(level) % 2 == 1:
                level = level + [level[-1]]
            level = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
            self.levels.append(level)

    @property
    def root(self) -> str:
        return self.levels[-1][0]

    def proof(self, index: int) -> MerkleProof:
        if not 0 <= index < len(self.leaves):
            raise IndexError(f"leaf index {index} out of range (batch size {len(self.leaves)})")
        path: list[tuple[str, str]] = []
        idx = index
        for level in self.levels[:-1]:
            nodes = level if len(level) % 2 == 0 else level + [level[-1]]
            if idx % 2 == 0:
                path.append((nodes[idx + 1], "right"))
            else:
                path.append((nodes[idx - 1], "left"))
            idx //= 2
        return MerkleProof(leaf_hash=self.leaves[index], path=path)
