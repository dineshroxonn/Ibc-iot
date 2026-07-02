# SensorChain on Hyperledger Fabric — Pune shard deployment

A real Fabric 2.5 network for one city shard, matching the DPoA design:
three mutually-distrusting validator organizations — **CitySPV**,
**Integrator** (system integrator), and **CPCB** (regulator) — each with
one endorsing peer, plus a Raft orderer. The channel's endorsement
policy is **MAJORITY**, so no single stakeholder can write provenance
records unilaterally.

The SensorChain chaincode (all five contracts: DeviceRegistry,
Calibration, Anchor, Firmware, SLA) runs as **chaincode-as-a-service**
using the `ccaas` external builder that ships in the fabric-peer image —
no in-peer docker builds needed.

## Requirements

Docker with the compose plugin. Everything else (cryptogen, configtxgen,
osnadmin, peer CLI) runs inside `hyperledger/fabric-tools` containers.
The chaincode must be compiled first:

```bash
cd ../../chaincode/sensorchain && npm install && npm run build
```

## Bring up + deploy

```bash
./network.sh up        # identities, genesis block, orderer + 3 peers, channel "pune"
./network.sh deploy    # package (ccaas) → install ×3 → approve ×3 → commit
./network.sh down      # stop and wipe generated material
```

## End-to-end lifecycle demo + benchmark

```bash
cd app && npm install
node e2e.js            # 9-step provenance lifecycle (below)
node e2e.js --bench    # + concurrent anchor throughput benchmark
```

The e2e connects via the **Fabric Gateway SDK** as a CitySPV client and
exercises, against the live network:

1. gateway registration (locally generated P-256 "HSM" keypair)
2. calibration certificate anchoring + status query
3. Merkle anchoring of a 60-reading batch, ECDSA-signed; the chaincode
   verifies the signature on-chain before accepting
4. on-chain Merkle verification — honest reading passes, tampered fails
5. forged anchor (wrong key) rejected by the chaincode
6. SLA registration → breaching metrics → `SLABreach` chaincode event
   caught live via the event stream
7. secure firmware upgrade: manufacturer-signed release → unapproved
   image refused → city approval → rollout attestation → trojaned
   install flagged on-chain
8. key rotation signed by the old key; old key loses anchor rights,
   new key gains them, history preserved on-chain
9. vendor transfer with history, then decommissioning — anchor rights
   cryptographically revoked

## Measured throughput

On a single laptop-class host running all four nodes plus the chaincode
container (`BENCH_TX=300 node e2e.js --bench`):

```
300/300 committed in 2.5–2.7s → ~113–118 TPS
```

Each transaction is a full anchor write: 3-org endorsement + on-chain
ECDSA signature verification + Raft ordering + commit on all peers.
A city shard needs ~12 TPS to anchor 700 gateways at 1-minute batches,
so a single modest host already provides ~10× headroom; dedicated
per-org hardware is expected to reach the 1,200 TPS/shard budget in the
proposal.
