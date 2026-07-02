# SensorChain — PoC Phase Report

**Blockchain India Challenge 2026 · Use Case 10: Blockchain Technology for trusted IoT/IIoT data and processes**
**Roxonn Future Tech Pvt. Ltd. · DPIIT DIPP230873 · Startup ID STU1772130451600D3ZKGDFA8**

---

## 1. Objective

The use case calls for an *immutable and shared ledger of IoT/IIoT generated events* giving all stakeholders *a single, verifiable source of data*, a *cryptographically secured chain of custody*, *automated compliance*, and innovative augmentation such as *device life cycle management* and *secure upgrade mechanisms*.

SensorChain delivers each of these as working, tested software — running on a real multi-organization Hyperledger Fabric network, fed by real MQTT device traffic, with every claim below backed by a reproducible measurement in this repository.

## 2. What was built and demonstrated

### 2.1 Shared ledger with a mutually-distrusting validator set

A Fabric 2.5 network for one city shard (`deployment/fabric/`): three organizations — **City SPV**, **System Integrator**, and **CPCB (regulator)** — each running an endorsing peer, with a Raft orderer and TLS throughout. The channel endorsement policy is **MAJORITY**: no single stakeholder, including the vendor, can write or alter provenance records unilaterally. One command brings the network up; a second deploys the chaincode with all three organizations' approval.

### 2.2 Cryptographic chain of custody

Custody is cryptographically continuous from silicon to citizen:

1. **Device identity**: each device's ECDSA P-256 keypair is generated in an HSM abstraction whose private key is never exportable; the public key, serial, BIS IS 17927 certificate (enforced — registration without it is refused), GPS, and vendor go on-chain at procurement.
2. **Calibration**: lab certificates are hashed and anchored per device; expired or missing calibration auto-flags the device.
3. **Data anchoring**: gateways batch readings per minute, build a domain-separated SHA-256 Merkle tree, and sign the anchor payload; **the chaincode verifies the signature against the registered key on-chain** before accepting. Only the 32-byte root is stored on-chain.
4. **Verification**: any reading is verifiable by any stakeholder — or any citizen, in-browser via WebCrypto — against the on-chain root in O(log n) hashes.

Demonstrated live on the Fabric network (`deployment/fabric/app/e2e.js`): honest readings verify; a tampered value fails its proof; an anchor signed with the wrong key is rejected by the chaincode with an `AnchorSignatureInvalid` audit event.

### 2.3 Automated compliance

- **Vendor SLA contract**: terms (data completeness, maximum sensor silence, calibration coverage) live on-chain. Period metrics are computed *from anchored data* — the same numbers any stakeholder can independently verify — and violations automatically emit `SLABreach` events with computed penalties. No vendor-controlled reporting step, no manual dispute.
- **Calibration compliance**: `UncalibratedDevice` events fire automatically on sweep; BIS certification is enforced at registration time.
- **AI anomaly oracle** (prototype layer): per-device autoencoders (temporal tampering/drift), sensor-silence detection, robust spatial divergence between co-located sensors, and cross-modal physics rules (e.g. severe vehicular-profile AQI on an empty road). Findings are anchored as on-chain events. In the pilot simulation, an AQI sensor under-reporting pollution by 60% was caught three independent ways: spatial z=26.8, autoencoder error 157× threshold, and the resulting SLA data-completeness breach.

### 2.4 Device life cycle management

All on-chain, all demonstrated live:

- **Key rotation** must be signed by the *current* device key (proof of possession); the old key is preserved in on-chain history so past anchors remain attributable. After rotation, the old key's anchors are rejected, the new key's accepted.
- **Vendor transfer** records an attributable vendor history — responsibility for any historical reading remains assignable to the vendor of record at that time.
- **Decommissioning** sets an irreversible retired status which the anchor contract enforces: anchor rights are cryptographically revoked.

### 2.5 Secure upgrade mechanism

A complete firmware chain of trust (`Firmware` contract), demonstrated live:

manufacturer signing key registered on-chain → release published only with a valid ECDSA signature (verified by the chaincode) → **city-SPV approval gate** before rollout → device verifies the image hash against the chain *before flashing* → post-flash attestation, where a mismatched hash auto-flags the device with a `FirmwareHashMismatch` event. In the live demo, an unapproved image was refused by the device-side check and a trojaned install was flagged on-chain.

### 2.6 Real device ingestion path

`edge/mqtt_gateway.py` is the production-shaped edge agent: it subscribes to the city MQTT broker, windows readings, Merkle-anchors through a local bridge into the live Fabric network, and retains the raw batches for proof serving. It carries no Fabric SDK, no numpy, no ML — MQTT, SHA-256, and one ECDSA signature per window.

## 3. Measured results

| Metric | Result | Where |
|---|---|---|
| Live-network lifecycle demo | 9/9 steps passed (registration → calibration → anchoring → proof/tamper → forged-anchor rejection → SLA breach event → firmware chain → key rotation → decommission) | `deployment/fabric/app/e2e.js` |
| Anchor throughput | **300/300 committed, ~113–118 TPS** on a single host — full 3-org endorsement + per-tx on-chain ECDSA verification | `node e2e.js --bench` |
| Shard requirement vs. measured | ~12 TPS needed (700 gateways × 1 anchor/min) → **~10× headroom on one machine** | — |
| MQTT ingestion run | 60 sensors, 1,740 readings, 5 batches anchored to the live network, **0 errors** | `edge/` |
| Edge agent footprint | **0.26% of one core, 29.3 MiB peak RSS** (target: <2% CPU, <50 MB) | measured by the agent itself |
| Off-chain/on-chain consistency | stored MQTT batch root == on-chain root; 360-reading batch proof = 9 hashes | verified post-run |
| Unit/integration tests | **51 passing** (Merkle, all 5 contracts, oracle, gateway, firmware, lifecycle, API) | `tests/` |
| Data minimisation | 1,085 simulated readings → 30 anchor tx × 32 bytes; raw data never leaves the city platform (DPDP-compatible, no PII on-chain) | `demo.py` |

## 4. IIoT applicability

The contracts are domain-neutral; the same deployment serves industrial process trust with only vocabulary changes:

| Smart city (deployed here) | Industrial / IIoT reading of the same contract |
|---|---|
| AQI/water/traffic sensor identity + BIS cert | Machine/instrument identity + calibration-lab traceability (ISO/IEC 17025) |
| Calibration certificate anchoring | Quality-control instrument calibration records for audits (e.g. pharma GxP, automotive IATF) |
| Merkle-anchored sensor batches | Tamper-evident process-parameter logs (temperature, pressure, torque) for batch-release documentation |
| Vendor SLA auto-breach | Supplier/OEM quality and uptime agreements with automated penalty events |
| Firmware chain of trust | Secure PLC/controller update governance — signed vendor releases, plant-owner approval gate, install attestation |
| Multi-org endorsement (SPV/integrator/regulator) | Plant owner / equipment OEM / certification body |
| Anomaly oracle | Predictive-quality deviation detection across co-located process sensors |

## 5. Remaining 6-week PoC plan

| Week | Work |
|---|---|
| 1 | Harden Fabric deployment (CA-based identities replacing cryptogen, per-org hosts); connect first physical gateway (Raspberry Pi) via the MQTT path |
| 2 | Wire the anomaly oracle + portal to the Fabric event stream (today they run on the prototype shard); Grafana stakeholder dashboard |
| 3 | Multi-shard demonstration: second city channel + national rollup channel anchoring both shard heads |
| 4 | Scale test toward the 1,200 TPS/shard budget on dedicated per-org hardware; CouchDB state + block-size tuning |
| 5 | Pilot-city data integration (CiDaP feed format), CPCB-format compliance exports, security review |
| 6 | Evaluation: demo video, final benchmarks, PoC report for C-DAC, stakeholder walkthrough |

## 6. Reproducing every claim

```bash
pip install -r requirements.txt && python3 -m pytest tests/   # 51 tests
python3 demo.py                                                # pilot audit report
cd chaincode/sensorchain && npm install && npm run build
cd ../../deployment/fabric && ./network.sh up && ./network.sh deploy
cd app && npm install && node e2e.js --bench                   # live lifecycle + TPS
node bridge.js &                                               # edge bridge
docker run -d --name mosquitto --network sensorchain -p 1883:1883 \
  -v $PWD/../mosquitto.conf:/mosquitto/config/mosquitto.conf eclipse-mosquitto:2
python3 edge/fleet_emulator.py --sensors 60 --cadence 5 --duration 150 &
python3 edge/mqtt_gateway.py --window 30 --duration 155        # prints footprint
```
