# SensorChain Architecture

## System overview

```
                        ┌────────────────────────────────────────────────┐
                        │                CITY DATA PLATFORM               │
  ┌─────────┐           │  ┌───────────────┐      ┌────────────────────┐ │
  │ sensors │──readings──▶ │ Gateway Anchor │      │  raw reading store │ │
  │ AQI/H2O/│           │  │ Agent (RPi 4)  │─────▶│  + proof service   │ │
  │ traffic │           │  │ 1-min batches  │      └─────────┬──────────┘ │
  └─────────┘           │  │ Merkle root    │                │            │
       HSM keypair      │  │ HSM signature  │                │ proofs     │
       at manufacture   │  └───────┬────────┘                │            │
                        └──────────┼─────────────────────────┼────────────┘
                                   │ anchor tx (32-byte root)│
                                   ▼                         ▼
                     ┌──────────────────────────┐   ┌──────────────────┐
                     │  CITY SHARD (3 DPoA      │   │ Citizen Portal   │
                     │  validator nodes)        │◀──│ WebCrypto Merkle │
                     │  · DeviceRegistry        │   │ proof checker    │
                     │  · Calibration           │   └──────────────────┘
                     │  · Anchor                │
                     │  · SLA                   │──events──▶ CPCB / SPV /
                     └────────────┬─────────────┘            integrator
                                  │ shard head hash
                                  ▼
                     ┌──────────────────────────┐   ┌──────────────────┐
                     │  NATIONAL ROLLUP CHAIN   │   │ AI Anomaly Oracle│
                     │  (elected validators,    │   │ autoencoder +    │
                     │  cross-city audit)       │   │ spatial + x-modal│
                     └──────────────────────────┘   └──────────────────┘
```

## Design decisions

### Anchoring, not storage

Sensor data volume (~2.5 TB/city/day) rules out on-chain storage. SensorChain anchors only:

- **device identity records** (once per device, at procurement)
- **calibration certificate hashes** (per calibration cycle)
- **one 32-byte Merkle root per gateway per minute**
- **anomaly and SLA-breach events**

Raw readings stay in the existing city platform. Integrity comes from the Merkle proof path: any published reading can be checked against the anchored root in O(log n) hashes. In the demo, 1,085 readings are covered by 30 anchor transactions.

At national scale: 100 cities × ~700 gateways × 1 tx/min ≈ 1,170 tx/s nationally, but **sharding makes this 12 tx/s per city shard** — comfortably inside Fabric's throughput envelope (the proposal budgets 1,200 TPS/shard).

### Canonical hashing across languages

Leaves are `SHA-256(0x00 ‖ canonical-JSON)` and internal nodes `SHA-256(0x01 ‖ left ‖ right)` (domain separation prevents leaf/node confusion attacks). Canonical JSON = sorted keys, no whitespace, UTF-8. Readings carry integer timestamps and fixed-decimal string values so Python, Node, and browser JavaScript produce byte-identical encodings — verified by a cross-language test.

### Device identity and the HSM contract

Each device's keypair is generated inside an HSM at manufacture; only `sign()` and the public key are exposed (mirrored by `SoftHSM` in the prototype, which never exports the private key). The registry contract **refuses registration without a BIS IS 17927 certificate reference**, pushing certification enforcement to procurement time. The anchor contract verifies each batch signature against the registered public key, so:

- an unregistered/retired gateway cannot anchor,
- a compromised server cannot forge anchors without the gateway key,
- an invalid signature is itself recorded as an `AnchorSignatureInvalid` event.

### City-cluster DPoA sharding

Each city is an independent shard with 3 delegated-authority validators (city SPV, system integrator, state/CPCB nominee — mutually distrusting parties). Consequences:

- a city's data integrity never depends on 99 other cities' consensus,
- PBFT-style O(n²) messaging is contained to 3 nodes per shard,
- the **national rollup chain** (elected validators) periodically anchors each shard's head block hash, so cross-city comparison and post-hoc audit don't require trusting any single city's operators.

The prototype's `Ledger` reproduces this with hash-chained blocks, round-robin validator attribution, world state, and events; `NationalRollup` anchors shard heads. `validate_chain()` recomputes every block hash — the tests demonstrate that editing a single committed transaction is detected.

### Anomaly oracle

Three independent detection levels, run over batches *as they are anchored*, with findings emitted as on-chain `AnomalyDetected` events (the detection record is itself tamper-evident):

1. **Temporal** — a per-device autoencoder (dense window→bottleneck→window, NumPy) trained on known-normal history; reconstruction error above a calibrated threshold flags tampering or drift. Silence detection flags devices that stop reporting.
2. **Spatial** — co-located sensors (grouped by GPS cell) are compared with a median/MAD robust z-score (with a relative floor so tight consensus doesn't inflate scores). An under-reporting AQI sensor stands out against its neighbours even if its own time series looks smooth.
3. **Cross-modal** — physics rules across modalities at one site, e.g. severe vehicular-profile AQI while the co-located traffic counter reads zero.

Duplicate suppression reports a persistent fault once per window rather than once per batch. In production the autoencoders are retrained quarterly on CiDaP data.

### SLA enforcement

SLA terms live on-chain per vendor. Period metrics — data completeness, longest silence, uncalibrated share — are computed **from the anchored batches**, so the numbers driving a breach are exactly the numbers any stakeholder can verify. Violations automatically emit `SLABreach` events with computed penalties; there is no vendor-controlled reporting step.

## Prototype → production mapping

| Prototype component | Production counterpart |
|---|---|
| `sensorchain.ledger.Ledger` | Hyperledger Fabric channel per city (3 endorsing peers, Raft ordering) |
| `sensorchain/contracts/*.py` | `chaincode/sensorchain` (TypeScript, fabric-contract-api) — already written and compiling |
| `SoftHSM` | TPM/SE on device, PKCS#11 HSM on gateways |
| `GatewayAgent` (Python) | static binary (<5 MB) on existing gateways; <2% CPU, <50 MB RAM on RPi 4 |
| FastAPI audit API | Fabric gateway service + REST facade, per city |
| `NationalRollup` | national Fabric channel with elected validators (NIC-hosted) |
| Simulator | live CiDaP / city platform feeds |

## Threat model coverage

| Attack | Mitigation |
|---|---|
| Value altered at dashboard/server layer | Merkle proof fails against the anchored root (demonstrated in `demo.py` §3 and the portal) |
| Forged anchor from compromised infrastructure | anchor signature verified against on-chain HSM public key |
| Sensor under-reports pollution | spatial + temporal oracle alerts, anchored as events (demo: aqi-003 caught at robust z=26.8 and 157× reconstruction threshold) |
| Sensor silently disabled | silence detection + SLA `max_silence_minutes` auto-breach |
| Uncalibrated sensor kept in service | on-chain calibration status, auto-flag events, SLA `max_uncalibrated_pct` breach |
| Vendor edits historical ledger copy | hash-chained blocks + national rollup anchor of shard heads |
| Replay/duplicate batch | anchor keyed by batch ID; duplicates rejected |

## Privacy & compliance

Only hashes, device metadata, and aggregate events go on-chain — no PII (DPDP Act compatible; environmental data is aggregated). Regulatory anchors: Smart Cities Mission Guidelines (MoHUA), NCAP/CPCB monitoring standards, BIS IS 17927 (enforced at registration), BEE smart metering, NIC CiDaP integration.
