"""Permissioned ledger for a city shard.

Development stand-in for a Hyperledger Fabric channel: hash-chained
blocks, a world state (key-value view of the latest transaction per
key), emitted events, and DPoA block signing by the shard's validator
set. The contract layer talks to this through the same put/get/query
interface Fabric chaincode uses, so the business logic ports directly
to the chaincode in `chaincode/`.

Each city runs its own shard (independent Ledger instance); a national
rollup chain periodically anchors every shard's latest block hash so
cross-city integrity does not depend on any single city's operator.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .merkle import canonical_json


@dataclass
class Transaction:
    tx_id: str
    contract: str
    method: str
    key: str
    value: dict
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id,
            "contract": self.contract,
            "method": self.method,
            "key": self.key,
            "value": self.value,
            "timestamp": self.timestamp,
        }


@dataclass
class Block:
    index: int
    prev_hash: str
    timestamp: float
    transactions: list[Transaction]
    validator: str
    block_hash: str = ""

    def compute_hash(self) -> str:
        payload = {
            "index": self.index,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "transactions": [t.to_dict() for t in self.transactions],
            "validator": self.validator,
        }
        return hashlib.sha256(canonical_json(payload)).hexdigest()

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "transactions": [t.to_dict() for t in self.transactions],
            "validator": self.validator,
            "block_hash": self.block_hash,
        }


@dataclass
class Event:
    name: str
    payload: dict
    block_index: int
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "payload": self.payload,
            "block_index": self.block_index,
            "timestamp": self.timestamp,
        }


GENESIS_HASH = "0" * 64


class Ledger:
    """One city shard: 3 DPoA validators, round-robin block proposal."""

    def __init__(self, shard_id: str, validators: list[str] | None = None):
        self.shard_id = shard_id
        self.validators = validators or [f"{shard_id}-validator-{i}" for i in range(1, 4)]
        self.blocks: list[Block] = []
        self.state: dict[str, dict] = {}
        self.events: list[Event] = []
        self._tx_counter = 0
        self._pending: list[Transaction] = []
        self._listeners: list[Callable[[Event], None]] = []

    # ------------------------------------------------------------------
    # Chaincode-facing interface (mirrors Fabric's ChaincodeStub)
    # ------------------------------------------------------------------
    def put_state(self, contract: str, method: str, key: str, value: dict) -> Transaction:
        # snapshot the value: committed blocks must be immune to later
        # mutation of the caller's dict
        value = json.loads(json.dumps(value))
        self._tx_counter += 1
        tx = Transaction(
            tx_id=f"{self.shard_id}-tx-{self._tx_counter:08d}",
            contract=contract,
            method=method,
            key=key,
            value=value,
            timestamp=time.time(),
        )
        self._pending.append(tx)
        self.state[key] = value
        self._commit_block()
        return tx

    def get_state(self, key: str) -> dict | None:
        return self.state.get(key)

    def query_by_prefix(self, prefix: str) -> list[tuple[str, dict]]:
        return sorted((k, v) for k, v in self.state.items() if k.startswith(prefix))

    def emit_event(self, name: str, payload: dict) -> Event:
        event = Event(
            name=name,
            payload=payload,
            block_index=len(self.blocks),
            timestamp=time.time(),
        )
        self.events.append(event)
        for listener in self._listeners:
            listener(event)
        return event

    def on_event(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    # ------------------------------------------------------------------
    # Block production (DPoA round-robin over the validator set)
    # ------------------------------------------------------------------
    def _commit_block(self) -> Block:
        prev_hash = self.blocks[-1].block_hash if self.blocks else GENESIS_HASH
        block = Block(
            index=len(self.blocks),
            prev_hash=prev_hash,
            timestamp=time.time(),
            transactions=self._pending,
            validator=self.validators[len(self.blocks) % len(self.validators)],
        )
        block.block_hash = block.compute_hash()
        self.blocks.append(block)
        self._pending = []
        return block

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------
    def validate_chain(self) -> tuple[bool, str]:
        """Recompute every block hash and check the prev-hash linkage."""
        prev = GENESIS_HASH
        for block in self.blocks:
            if block.prev_hash != prev:
                return False, f"block {block.index}: broken prev_hash link"
            if block.compute_hash() != block.block_hash:
                return False, f"block {block.index}: block hash mismatch (tampered)"
            prev = block.block_hash
        return True, f"{len(self.blocks)} blocks verified"

    def head_hash(self) -> str:
        return self.blocks[-1].block_hash if self.blocks else GENESIS_HASH

    def export(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            for block in self.blocks:
                fh.write(json.dumps(block.to_dict()) + "\n")


class NationalRollup:
    """National rollup chain: anchors each city shard's head block hash.

    Cross-city comparison and audit ride on this chain, while each
    city's data integrity remains independent of the other 99 shards.
    """

    def __init__(self) -> None:
        self.ledger = Ledger("national-rollup", validators=[f"rollup-validator-{i}" for i in range(1, 6)])

    def anchor_shard(self, shard: Ledger) -> Transaction:
        return self.ledger.put_state(
            contract="rollup",
            method="anchorShardHead",
            key=f"shard~{shard.shard_id}~{len(shard.blocks)}",
            value={
                "shard_id": shard.shard_id,
                "height": len(shard.blocks),
                "head_hash": shard.head_hash(),
            },
        )

    def shard_anchors(self, shard_id: str) -> list[dict[str, Any]]:
        return [v for _, v in self.ledger.query_by_prefix(f"shard~{shard_id}~")]
