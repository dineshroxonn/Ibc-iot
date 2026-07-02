#!/usr/bin/env python3
"""SensorChain end-to-end demonstration.

Runs the full pilot scenario on one city shard and prints an audit
report:

  1. device registration (BIS-certified, HSM keypairs) + calibration
  2. 30 minutes of simulated sensor traffic, Merkle-anchored per minute
  3. tamper demonstration: an altered dashboard value fails its proof
  4. AI oracle findings (temporal / silence / spatial / cross-modal)
  5. automatic SLA breach events with penalties
  6. shard chain validation + national rollup anchors

Usage:  python3 demo.py [minutes]
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

from sensorchain.contracts import AnchorContract
from sensorchain.demo_city import build_demo_city
from sensorchain.merkle import MerkleProof, hash_leaf


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    print("SensorChain — Smart City IoT Data Integrity & Provenance")
    print(f"Pilot scenario: Pune shard, {minutes:.0f} simulated minutes")

    city = build_demo_city(minutes)
    ledger = city.ledger

    # ------------------------------------------------------------------
    section("1 · Device Identity Registry (on-chain, HSM-backed)")
    by_vendor: dict[str, list] = defaultdict(list)
    for device in city.devices:
        by_vendor[device.vendor].append(device)
    for vendor, fleet in sorted(by_vendor.items()):
        names = ", ".join(d.device_id for d in fleet)
        print(f"  {vendor:<8} {len(fleet)} devices: {names}")
    print(f"\n  uncalibrated devices auto-flagged: "
          f"{[f['device_id'] for f in city.flagged_uncalibrated]} "
          f"(reason: {city.flagged_uncalibrated[0]['reason']})")

    # ------------------------------------------------------------------
    section("2 · Merkle-Root Batch Anchoring")
    batches = city.agent.batches
    total_readings = sum(len(b.readings) for b in batches.values())
    print(f"  anchored batches   : {len(batches)} (one 60 s window each)")
    print(f"  readings covered   : {total_readings}")
    print(f"  on-chain footprint : {len(batches)} tx × 32-byte root "
          f"(raw data stays in the city platform)")
    sample = next(iter(batches.values()))
    print(f"  example            : {sample.batch_id} root={sample.merkle_root[:24]}… "
          f"({len(sample.readings)} readings)")

    # ------------------------------------------------------------------
    section("3 · Tamper Demonstration (citizen Merkle proof check)")
    anchor_contract = AnchorContract(ledger)
    batch = sample
    package = city.agent.proof_for_reading(batch.batch_id, 3)
    proof = MerkleProof.from_dict(package["proof"])
    honest = anchor_contract.verify_reading(batch.batch_id, proof)
    reading = package["reading"]
    print(f"  reading            : {reading['device_id']} = {reading['value']} {reading['unit']}")
    print(f"  honest reading     : verified={honest['verified']}")

    tampered_reading = dict(reading, value="42.000")     # dashboard-layer manipulation
    forged = MerkleProof(leaf_hash=hash_leaf(tampered_reading), path=proof.path)
    tampered = anchor_contract.verify_reading(batch.batch_id, forged)
    print(f"  tampered (42.000)  : verified={tampered['verified']} — {tampered['reason']}")

    # ------------------------------------------------------------------
    section("4 · AI Anomaly Oracle (temporal / silence / spatial / cross-modal)")
    counts = Counter(a.kind for a in city.oracle.anomalies)
    print(f"  alerts: {dict(counts)}\n")
    for anomaly in city.oracle.anomalies:
        flag = "CRIT" if anomaly.severity == "critical" else "warn"
        print(f"  [{flag}] {anomaly.kind:<11} {anomaly.device_id:<28} {anomaly.detail}")

    # ------------------------------------------------------------------
    section("5 · Vendor SLA Enforcement (smart-contract auto-breach)")
    for report in city.sla_reports:
        status = "BREACH" if report["breached"] else "ok    "
        print(f"  [{status}] {report['vendor']:<8} completeness={report['metrics']['completeness_pct']}%  "
              f"max_silence={report['metrics']['max_silence_minutes']}min  "
              f"uncalibrated={report['metrics']['uncalibrated_pct']}%")
        for violation in report["violations"]:
            print(f"            ↳ {violation['term']}: required {violation['required']}, "
                  f"observed {violation['observed']}")
        if report["breached"]:
            print(f"            ↳ penalty ₹{report['penalty_inr']:,}")
    breach_events = [e for e in ledger.events if e.name == "SLABreach"]
    print(f"\n  SLABreach events emitted on-chain: {len(breach_events)}")

    # ------------------------------------------------------------------
    section("6 · Chain Integrity + National Rollup")
    ok, detail = ledger.validate_chain()
    print(f"  shard '{ledger.shard_id}' ({len(ledger.validators)} DPoA validators): "
          f"{'VALID' if ok else 'INVALID'} — {detail}")
    print(f"  head hash          : {ledger.head_hash()}")
    print(f"  events on-chain    : {Counter(e.name for e in ledger.events).most_common()}")
    anchors = city.rollup.shard_anchors("pune")
    print(f"  national rollup    : {len(anchors)} shard-head anchors "
          f"(latest height {anchors[-1]['height']})")
    match = anchors[-1]["head_hash"] == ledger.head_hash()
    print(f"  rollup ↔ shard head: {'consistent' if match else 'MISMATCH'}")

    print("\nDone. Start the API + citizen portal with:")
    print("  uvicorn sensorchain.api:app --port 8000   →  http://localhost:8000/")


if __name__ == "__main__":
    main()
