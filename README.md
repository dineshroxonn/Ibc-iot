# SensorChain — Smart City IoT Data Integrity & Provenance Platform

**Blockchain India Challenge 2026 · Use Case: IoT/IIoT · Roxonn Future Tech Pvt. Ltd. (DPIIT DIPP230873)**

India's Smart Cities Mission has deployed 4M+ IoT sensors, yet a dashboard that reports *AQI = 85* offers no verifiable record of which sensor produced it, whether that sensor was calibrated, or whether the value was altered at the gateway, server, or dashboard layer. SensorChain closes that gap: a permissioned blockchain anchors device identities, calibration certificates, and per-minute Merkle roots of sensor batches — so the city SPV, CPCB, and any citizen can verify the same data independently of the vendor.

This repository is a working implementation of all six modules from the proposal — plus a secure firmware-upgrade chain of trust and full device lifecycle management — **deployed and benchmarked on a real 3-org Hyperledger Fabric network**.

## Quick start

```bash
pip install -r requirements.txt

python3 -m pytest tests/          # 51 tests
python3 demo.py                   # full pilot scenario, printed audit report
uvicorn sensorchain.api:app --port 8000   # REST API + citizen portal at http://localhost:8000/
```

Real Fabric network (Docker required — see [deployment/fabric/README.md](deployment/fabric/README.md)):

```bash
cd chaincode/sensorchain && npm install && npm run build && cd ../../deployment/fabric
./network.sh up && ./network.sh deploy    # 3 orgs (CitySPV, Integrator, CPCB) + Raft orderer
cd app && npm install && node e2e.js --bench   # 9-step lifecycle + throughput benchmark
```

Real device ingestion — MQTT fleet → edge gateway agent → live Fabric anchors:

```bash
node deployment/fabric/app/bridge.js &         # localhost HTTP → Fabric Gateway bridge
python3 edge/fleet_emulator.py --sensors 60 &  # publishes readings over MQTT
python3 edge/mqtt_gateway.py                   # windows, signs, anchors; prints CPU/RSS footprint
```

Measured on this pipeline: 60 sensors, 1,740 readings, 5 anchored batches, 0 errors — at **0.26% CPU and 29.3 MiB peak RSS** on the edge agent (target from the proposal: <2% CPU, <50 MB). Full evidence and the 6-week plan are in [docs/POC_REPORT.md](docs/POC_REPORT.md), including the IIoT mapping of every contract (machine identity, QC calibration records, PLC update governance, supplier SLAs).

`demo.py` runs the entire lifecycle on a simulated Pune shard: 9 devices across 3 vendors are registered with BIS certificates and HSM keypairs, calibrated (one deliberately skipped and auto-flagged), then 30 minutes of AQI / water-quality / traffic readings are batched and Merkle-anchored per minute. Injected faults — a pollution under-reporting sensor, a dead traffic counter under severe AQI, a water sensor going dark — are caught by the anomaly oracle, SLA breaches fire automatically with penalties, and a tampered dashboard value is shown failing its Merkle proof.

## The six modules

| Module | Where | What it does |
|---|---|---|
| Device Identity Registry | `sensorchain/contracts/device_registry.py` | On-chain identity: serial, manufacturer, **BIS IS 17927 cert (enforced)**, GPS, HSM public key. Full lifecycle: key rotation (authorized by the old key, history preserved), vendor transfer with history, decommissioning that revokes anchor rights. |
| Calibration Certificate Anchor | `sensorchain/contracts/calibration.py` | Lab-signed certificate hashes anchored per device; expired/missing calibration auto-flagged on-chain. |
| Gateway Anchor Agent | `sensorchain/gateway/agent.py` | Buffers readings into 1-minute windows, computes the Merkle root, signs with the gateway HSM, anchors one small tx per window. Raw data never leaves the city platform. |
| AI Anomaly Detection Oracle | `sensorchain/oracle/` | NumPy autoencoder per device (temporal tampering/drift) + sensor-silence detection + robust spatial divergence between co-located sensors + cross-modal physics rules (severe vehicular AQI on an empty road). Findings anchored as on-chain events. |
| SLA Enforcement Contract | `sensorchain/contracts/sla.py` | Vendor SLA encoded on-chain (completeness, max silence, calibration coverage). Metrics computed from *anchored* data → automatic `SLABreach` events with penalties. |
| Public Verification Portal | `portal/index.html` | Citizen Merkle proof checker. Verification runs entirely in the browser via WebCrypto — byte-identical canonical hashing to the Python side. |
| Secure Firmware Upgrade | `sensorchain/contracts/firmware.py` | Manufacturer signing keys on-chain → ECDSA-verified release publication → city approval gate → device-side hash verification before flashing → post-flash attestation with auto-flagging of trojaned installs. |

Supporting infrastructure:

- `sensorchain/merkle.py` — domain-separated SHA-256 Merkle trees, O(log n) inclusion proofs
- `sensorchain/identity.py` — HSM abstraction (ECDSA P-256; private key never exported)
- `sensorchain/ledger.py` — city-shard ledger (hash-chained blocks, world state, events, 3 DPoA validators) + **national rollup chain** anchoring every shard's head hash
- `sensorchain/simulator.py` / `sensorchain/demo_city.py` — realistic fleet simulation with injectable fault scenarios
- `sensorchain/api.py` — the open audit API (devices, anchors, proofs, anomalies, SLA, chain validation)
- `chaincode/sensorchain/` — production **Hyperledger Fabric chaincode (TypeScript)** mirroring all five contracts, including on-chain ECDSA verification of gateway anchor and firmware signatures
- `deployment/fabric/` — a **real Fabric 2.5 network** (3 orgs + Raft orderer, TLS, MAJORITY endorsement) with one-command bring-up, chaincode-as-a-service deployment, a 9-step Gateway-SDK lifecycle demo, and a measured **~118 TPS** anchor benchmark (300/300 committed, single host)

## Citizen verification portal

Fetch any anchored reading and its proof, then verify locally — or tamper with one field and watch it fail:

![Portal verifying a reading against the on-chain Merkle root](docs/images/portal_verified.png)

## How verification works

1. Every reading is hashed as `SHA-256(0x00 ‖ canonical-JSON)`; pairs combine as `SHA-256(0x01 ‖ left ‖ right)` up to the batch root.
2. The gateway signs `gateway|batch|root|window|count` with its HSM key; the anchor contract verifies this against the public key registered at procurement before accepting.
3. Only the 32-byte root goes on-chain (~1085 readings → 30 anchor transactions in the demo).
4. Anyone holding a reading + Merkle path recomputes the root in O(log n) and compares it with the on-chain anchor. An altered value, timestamp, or device ID changes the leaf hash and the proof fails.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design: city-cluster DPoA sharding (each city an independent 3-node shard, national rollup for cross-city audit), data flow, trust model, and the mapping from prototype components to the production Fabric deployment.

## Repository layout

```
sensorchain/          core Python package (contracts, gateway, oracle, ledger, API)
chaincode/sensorchain production Hyperledger Fabric chaincode (TypeScript, 5 contracts)
deployment/fabric/    real 3-org Fabric network + Gateway SDK e2e demo + benchmark
portal/               citizen verification portal (self-contained HTML + WebCrypto)
tests/                51 pytest tests (merkle, contracts, oracle, gateway, firmware, API)
demo.py               end-to-end pilot scenario with printed audit report
docs/                 architecture documentation and screenshots
```
