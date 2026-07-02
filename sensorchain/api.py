"""SensorChain REST API — the open audit interface.

Serves the city shard's on-chain state (devices, calibrations, anchors,
SLA reports, anomaly events, blocks) plus the off-chain proof service,
so that CPCB, the city SPV, and citizens can all verify the same data
without going through the vendor.

Run:  uvicorn sensorchain.api:app --reload
The instance boots with a small demo city so every endpoint is live
immediately; swap `build_demo_city` for real Fabric gateway bindings
in production.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel

from .contracts import AnchorContract, CalibrationContract, DeviceRegistryContract, SLAContract
from .demo_city import DemoCity, build_demo_city
from .merkle import MerkleProof

app = FastAPI(
    title="SensorChain",
    description="Smart City IoT Data Integrity & Provenance — open audit API",
    version="0.1.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

city: DemoCity = build_demo_city()

PORTAL_INDEX = Path(__file__).resolve().parent.parent / "portal" / "index.html"


class VerifyRequest(BaseModel):
    batch_id: str
    reading: dict
    proof: dict


# ----------------------------------------------------------------------
# Portal
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def portal():
    return FileResponse(PORTAL_INDEX)


# ----------------------------------------------------------------------
# Device registry & calibration
# ----------------------------------------------------------------------
@app.get("/api/devices")
def list_devices(vendor: str | None = None):
    registry = DeviceRegistryContract(city.ledger)
    calibration = CalibrationContract(city.ledger)
    devices = registry.list_devices(vendor)
    return [
        {**{k: v for k, v in d.items() if k != "public_key_pem"},
         "calibration": calibration.calibration_status(d["device_id"])}
        for d in devices
    ]


@app.get("/api/devices/{device_id}")
def get_device(device_id: str):
    device = DeviceRegistryContract(city.ledger).get_device(device_id)
    if device is None:
        raise HTTPException(404, f"device {device_id} not registered")
    device["calibration"] = CalibrationContract(city.ledger).calibration_status(device_id)
    return device


# ----------------------------------------------------------------------
# Anchors & proofs
# ----------------------------------------------------------------------
@app.get("/api/anchors")
def list_anchors(gateway_id: str | None = None):
    return AnchorContract(city.ledger).list_anchors(gateway_id)


@app.get("/api/anchors/{batch_id}")
def get_anchor(batch_id: str):
    anchor = AnchorContract(city.ledger).get_anchor(batch_id)
    if anchor is None:
        raise HTTPException(404, f"no anchor for batch {batch_id}")
    return anchor


@app.get("/api/proofs/{batch_id}/{reading_index}")
def get_proof(batch_id: str, reading_index: int):
    """Off-chain proof service: reading + Merkle path for independent checking."""
    batch = city.agent.batches.get(batch_id)
    if batch is None:
        raise HTTPException(404, f"batch {batch_id} not held by this gateway")
    if not 0 <= reading_index < len(batch.readings):
        raise HTTPException(404, f"reading index out of range (batch size {len(batch.readings)})")
    return city.agent.proof_for_reading(batch_id, reading_index)


@app.post("/api/verify")
def verify(request: VerifyRequest):
    """Citizen verification: does (reading, proof) match the on-chain root?"""
    from .merkle import hash_leaf

    proof = MerkleProof.from_dict(request.proof)
    if hash_leaf(request.reading) != proof.leaf_hash:
        return {"verified": False,
                "reason": "reading does not hash to the proof's leaf — reading altered"}
    return AnchorContract(city.ledger).verify_reading(request.batch_id, proof)


# ----------------------------------------------------------------------
# Anomalies, SLA, chain
# ----------------------------------------------------------------------
@app.get("/api/anomalies")
def list_anomalies():
    return [a.to_dict() for a in city.oracle.anomalies]


@app.get("/api/sla/{vendor}")
def get_sla(vendor: str):
    sla = SLAContract(city.ledger).get_sla(vendor)
    if sla is None:
        raise HTTPException(404, f"no SLA registered for {vendor}")
    return sla


@app.get("/api/sla-breaches")
def list_breaches(vendor: str | None = None):
    return SLAContract(city.ledger).breaches(vendor)


@app.get("/api/chain/blocks")
def list_blocks(offset: int = 0, limit: int = 20):
    blocks = city.ledger.blocks[offset:offset + limit]
    return {"total": len(city.ledger.blocks), "blocks": [b.to_dict() for b in blocks]}


@app.get("/api/chain/validate")
def validate_chain():
    ok, detail = city.ledger.validate_chain()
    return {"valid": ok, "detail": detail, "head_hash": city.ledger.head_hash(),
            "shard_id": city.ledger.shard_id}


@app.get("/api/chain/events")
def list_events(name: str | None = None):
    events = city.ledger.events
    if name is not None:
        events = [e for e in events if e.name == name]
    return [e.to_dict() for e in events]


@app.get("/api/rollup/{shard_id}")
def rollup_anchors(shard_id: str):
    return city.rollup.shard_anchors(shard_id)
