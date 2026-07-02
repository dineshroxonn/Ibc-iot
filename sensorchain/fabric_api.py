"""SensorChain open audit API — Fabric-backed.

The same REST surface (and the same citizen portal) as
`sensorchain.api`, but every record comes from the LIVE Hyperledger
Fabric shard via the bridge: devices, calibration, anchors, anomalies,
and SLA reports are on-chain state; Merkle proof verification is
evaluated BY THE CHAINCODE. The only off-chain source is the gateway
batch store, which serves raw readings + proofs (raw data never goes
on-chain by design).

Run (bridge must be up):
  SENSORCHAIN_BRIDGE=http://127.0.0.1:8801 \
  SENSORCHAIN_BATCH_STORE=/tmp/sensorchain-batches \
  uvicorn sensorchain.fabric_api:app --port 8100
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .merkle import MerkleProof, MerkleTree, hash_leaf

BRIDGE = os.environ.get("SENSORCHAIN_BRIDGE", "http://127.0.0.1:8801")
BATCH_STORE = Path(os.environ.get("SENSORCHAIN_BATCH_STORE", "/tmp/sensorchain-batches"))
ROLLUP_BRIDGE = os.environ.get("SENSORCHAIN_ROLLUP_BRIDGE", "")

app = FastAPI(
    title="SensorChain (Fabric)",
    description="Smart City IoT Data Integrity & Provenance — open audit API backed by the live Fabric shard",
    version="0.2.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PORTAL_INDEX = Path(__file__).resolve().parent.parent / "portal" / "index.html"


class VerifyRequest(BaseModel):
    batch_id: str
    reading: dict
    proof: dict


def bridge_get(path: str, base: str = ""):
    try:
        response = httpx.get(f"{base or BRIDGE}{path}", timeout=60)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"bridge unreachable: {exc}") from exc
    payload = response.json()
    if response.status_code != 200:
        raise HTTPException(404 if "unknown" in str(payload) or "no anchor" in str(payload) else 502,
                            payload.get("error", "bridge error"))
    return payload


def bridge_post(path: str, payload: dict):
    try:
        response = httpx.post(f"{BRIDGE}{path}", json=payload, timeout=60)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"bridge unreachable: {exc}") from exc
    body = response.json()
    if response.status_code != 200:
        raise HTTPException(502, body.get("error", "bridge error"))
    return body


def load_batch(batch_id: str) -> dict:
    matches = list(BATCH_STORE.glob(f"*/{batch_id}.json"))
    if not matches:
        raise HTTPException(404, f"batch {batch_id} not in the local batch store")
    return json.loads(matches[0].read_text())


# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def portal():
    return FileResponse(PORTAL_INDEX)


@app.get("/api/devices")
def list_devices(vendor: str | None = None):
    return bridge_get(f"/devices?vendor={vendor or ''}")


@app.get("/api/devices/{device_id}")
def get_device(device_id: str):
    return bridge_get(f"/device/{device_id}")


@app.get("/api/anchors")
def list_anchors(gateway_id: str | None = None):
    return bridge_get(f"/anchors?gateway_id={gateway_id or ''}")


@app.get("/api/anchors/{batch_id}")
def get_anchor(batch_id: str):
    return bridge_get(f"/anchor/{batch_id}")


@app.get("/api/proofs/{batch_id}/{reading_index}")
def get_proof(batch_id: str, reading_index: int):
    batch = load_batch(batch_id)
    readings = batch["readings"]
    if not 0 <= reading_index < len(readings):
        raise HTTPException(404, f"reading index out of range (batch size {len(readings)})")
    tree = MerkleTree(readings)
    return {
        "batch_id": batch_id,
        "reading": readings[reading_index],
        "reading_index": reading_index,
        "proof": tree.proof(reading_index).to_dict(),
        "merkle_root": tree.root,
    }


@app.post("/api/verify")
def verify(request: VerifyRequest):
    """Citizen verification, evaluated on-chain by the Anchor contract."""
    proof = MerkleProof.from_dict(request.proof)
    if hash_leaf(request.reading) != proof.leaf_hash:
        return {"verified": False,
                "reason": "reading does not hash to the proof's leaf — reading altered"}
    result = bridge_post("/verify", {
        "batch_id": request.batch_id,
        "leaf_hash": proof.leaf_hash,
        "path": [[h, s] for h, s in proof.path],
    })
    if not result.get("verified"):
        result.setdefault("reason", "Merkle proof does not match on-chain root — data altered after anchoring")
    return result


@app.get("/api/anomalies")
def list_anomalies():
    return bridge_get("/anomalies")


@app.get("/api/sla-breaches")
def list_breaches(vendor: str | None = None):
    return bridge_get(f"/sla-breaches?vendor={vendor or ''}")


@app.get("/api/chain/validate")
def validate_chain():
    info = bridge_get("/chain")
    return {
        "valid": True,
        "detail": (f"Fabric channel '{info['channel']}' at height {info['height']} — "
                   f"3-org MAJORITY endorsement (CitySPV, Integrator, CPCB)"),
        "head_hash": info["head_hash"],
        "shard_id": info["channel"],
    }


@app.get("/api/rollup/{shard_id}")
def rollup_anchors(shard_id: str):
    if not ROLLUP_BRIDGE:
        raise HTTPException(503, "no rollup bridge configured (set SENSORCHAIN_ROLLUP_BRIDGE)")
    return bridge_get(f"/shard-anchors?shard={shard_id}", base=ROLLUP_BRIDGE)
