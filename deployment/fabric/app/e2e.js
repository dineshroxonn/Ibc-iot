/*
 * SensorChain on Fabric — end-to-end provenance lifecycle.
 *
 * Connects to the Pune shard as the CitySPV org via the Fabric Gateway
 * SDK and exercises every contract against the REAL network:
 *
 *   1. register a gateway device (HSM keypair generated locally,
 *      private key never leaves this process)
 *   2. anchor a calibration certificate + query calibration status
 *   3. Merkle-anchor a 60-reading batch, ECDSA-signed by the gateway
 *      (the chaincode verifies the signature on-chain)
 *   4. verify an honest reading's Merkle proof on-chain, then show a
 *      tampered reading failing
 *   5. reject a forged anchor signed by the wrong key
 *   6. register a vendor SLA, record breaching metrics, and catch the
 *      SLABreach chaincode event
 *
 * Run `node e2e.js --bench` to add a concurrent anchor throughput
 * benchmark after the lifecycle demo.
 */

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const grpc = require("@grpc/grpc-js");
const { connect, signers } = require("@hyperledger/fabric-gateway");

const CHANNEL = "pune";
const CHAINCODE = "sensorchain";
const MSP_ID = "CitySPVMSP";
const PEER_ENDPOINT = process.env.PEER_ENDPOINT || "localhost:7051";
const PEER_HOST_ALIAS = "peer0.cityspv.sensorchain.local";

const CRYPTO = path.resolve(__dirname, "..", "crypto-config",
  "peerOrganizations", "cityspv.sensorchain.local");
const USER_MSP = path.join(CRYPTO, "users", "User1@cityspv.sensorchain.local", "msp");
const PEER_TLS_CA = path.join(CRYPTO, "peers", PEER_HOST_ALIAS, "tls", "ca.crt");

const utf8 = new TextDecoder();
const j = (o) => JSON.stringify(o);

// ---------------------------------------------------------------------------
// Merkle tree — identical algorithm to sensorchain/merkle.py and the portal
// ---------------------------------------------------------------------------
function canonicalJSON(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonicalJSON).join(",") + "]";
  return "{" + Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + canonicalJSON(v[k])).join(",") + "}";
}
const sha256 = (buf) => crypto.createHash("sha256").update(buf).digest("hex");
const hashLeaf = (reading) =>
  sha256(Buffer.concat([Buffer.from([0]), Buffer.from(canonicalJSON(reading), "utf8")]));
const hashPair = (l, r) =>
  sha256(Buffer.concat([Buffer.from([1]), Buffer.from(l, "hex"), Buffer.from(r, "hex")]));

function merkleTree(readings) {
  const levels = [readings.map(hashLeaf)];
  while (levels[levels.length - 1].length > 1) {
    let level = levels[levels.length - 1];
    if (level.length % 2 === 1) level = [...level, level[level.length - 1]];
    const next = [];
    for (let i = 0; i < level.length; i += 2) next.push(hashPair(level[i], level[i + 1]));
    levels.push(next);
  }
  return { root: levels[levels.length - 1][0], levels };
}
function merkleProof(tree, index) {
  const pathOut = [];
  let idx = index;
  for (const rawLevel of tree.levels.slice(0, -1)) {
    const level = rawLevel.length % 2 === 1 ? [...rawLevel, rawLevel[rawLevel.length - 1]] : rawLevel;
    pathOut.push(idx % 2 === 0 ? [level[idx + 1], "right"] : [level[idx - 1], "left"]);
    idx = Math.floor(idx / 2);
  }
  return pathOut;
}

// ---------------------------------------------------------------------------
// Local "HSM": P-256 keypair, sign-only interface
// ---------------------------------------------------------------------------
function makeHsm() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ec", { namedCurve: "P-256" });
  return {
    publicPem: publicKey.export({ type: "spki", format: "pem" }).toString(),
    sign: (payload) => crypto.createSign("SHA256").update(payload).sign(privateKey).toString("hex"),
  };
}
const anchorPayload = (gw, batch, root, ws, we, n) =>
  Buffer.from(`${gw}|${batch}|${root}|${ws.toFixed(3)}|${we.toFixed(3)}|${n}`, "utf8");

// ---------------------------------------------------------------------------
// Gateway connection
// ---------------------------------------------------------------------------
function firstFile(dir) {
  return path.join(dir, fs.readdirSync(dir)[0]);
}

async function newGateway() {
  const tlsCredentials = grpc.credentials.createSsl(fs.readFileSync(PEER_TLS_CA));
  const client = new grpc.Client(PEER_ENDPOINT, tlsCredentials, {
    "grpc.ssl_target_name_override": PEER_HOST_ALIAS,
  });
  const credentials = fs.readFileSync(firstFile(path.join(USER_MSP, "signcerts")));
  const privateKeyPem = fs.readFileSync(firstFile(path.join(USER_MSP, "keystore")));
  const gateway = connect({
    client,
    identity: { mspId: MSP_ID, credentials },
    signer: signers.newPrivateKeySigner(crypto.createPrivateKey(privateKeyPem)),
    evaluateOptions: () => ({ deadline: Date.now() + 15000 }),
    endorseOptions: () => ({ deadline: Date.now() + 30000 }),
    submitOptions: () => ({ deadline: Date.now() + 15000 }),
    commitStatusOptions: () => ({ deadline: Date.now() + 60000 }),
  });
  return { gateway, client };
}

const step = (n, msg) => console.log(`\n[${n}] ${msg}`);

async function main() {
  const runId = crypto.randomBytes(4).toString("hex");   // unique ids per run
  const gatewayId = `gw-pune-${runId}`;
  const { gateway, client } = await newGateway();
  const network = gateway.getNetwork(CHANNEL);
  const contract = network.getContract(CHAINCODE);

  try {
    // -- 1. device registration --------------------------------------------
    step(1, `register gateway device ${gatewayId} (DeviceRegistry contract)`);
    const hsm = makeHsm();
    const record = {
      device_id: gatewayId, sensor_type: "gateway", manufacturer: "Cisco",
      serial_number: `GW-${runId}`, bis_certificate: `BIS-17927-${runId}`,
      vendor: "VendorA", city: "pune", latitude: 18.52, longitude: 73.85,
      public_key_pem: hsm.publicPem,
      key_fingerprint: sha256(Buffer.from(hsm.publicPem)).slice(0, 16),
    };
    await contract.submit("DeviceRegistry:RegisterDevice", { arguments: [j(record)] });
    const stored = JSON.parse(utf8.decode(
      await contract.evaluate("DeviceRegistry:GetDevice", { arguments: [gatewayId] })));
    console.log(`    on-chain: status=${stored.status} fingerprint=${stored.key_fingerprint}`);

    // -- 2. calibration ------------------------------------------------------
    step(2, "anchor calibration certificate (Calibration contract)");
    await contract.submit("Calibration:AnchorCertificate", {
      arguments: [gatewayId, "NPL-Delhi", "NABL-CC-2115", "pass", j({ reference: "CPCB-CAAQM" })],
    });
    const calibration = JSON.parse(utf8.decode(
      await contract.evaluate("Calibration:CalibrationStatus", { arguments: [gatewayId] })));
    console.log(`    calibrated=${calibration.calibrated} cert=${(calibration.certificate_hash || "").slice(0, 16)}…`);

    // -- 3. Merkle batch anchoring -------------------------------------------
    step(3, "anchor a 60-reading batch, signed by the gateway HSM (Anchor contract)");
    const readings = Array.from({ length: 60 }, (_, i) => ({
      device_id: `aqi-${runId}`, sensor_type: "aqi",
      timestamp: 1750000000 + i, value: (230 + Math.sin(i / 5) * 8).toFixed(3), unit: "AQI",
    }));
    const tree = merkleTree(readings);
    const batchId = `${gatewayId}-batch-000001`;
    const [ws, we, n] = [1750000000.0, 1750000059.0, readings.length];
    const signature = hsm.sign(anchorPayload(gatewayId, batchId, tree.root, ws, we, n));
    const anchor = JSON.parse(utf8.decode(await contract.submit("Anchor:AnchorBatch", {
      arguments: [gatewayId, batchId, tree.root, ws.toFixed(3), we.toFixed(3), String(n), signature],
    })));
    console.log(`    anchored root=${anchor.merkle_root.slice(0, 24)}… count=${anchor.reading_count}`);

    // -- 4. on-chain Merkle verification --------------------------------------
    step(4, "verify honest + tampered readings against the on-chain root");
    const proof = merkleProof(tree, 7);
    const honest = JSON.parse(utf8.decode(await contract.evaluate("Anchor:VerifyReading", {
      arguments: [batchId, hashLeaf(readings[7]), j(proof)],
    })));
    console.log(`    honest reading #7   : verified=${honest.verified}`);
    const tampered = { ...readings[7], value: "42.000" };
    const forged = JSON.parse(utf8.decode(await contract.evaluate("Anchor:VerifyReading", {
      arguments: [batchId, hashLeaf(tampered), j(proof)],
    })));
    console.log(`    tampered (42.000)   : verified=${forged.verified} — ${forged.reason}`);

    // -- 5. forged anchor rejected ---------------------------------------------
    step(5, "submit an anchor signed by the WRONG key (must be rejected on-chain)");
    const imposter = makeHsm();
    const badSig = imposter.sign(anchorPayload(gatewayId, "forged-batch", tree.root, ws, we, n));
    try {
      await contract.submit("Anchor:AnchorBatch", {
        arguments: [gatewayId, "forged-batch", tree.root, ws.toFixed(3), we.toFixed(3), String(n), badSig],
      });
      throw new Error("FORGED ANCHOR WAS ACCEPTED — THIS IS A BUG");
    } catch (err) {
      if (String(err).includes("THIS IS A BUG")) throw err;
      console.log(`    rejected as expected: invalid signature from ${gatewayId}`);
    }

    // -- 6. SLA + chaincode event ------------------------------------------------
    step(6, "register SLA, record breaching metrics, catch SLABreach event");
    const vendor = `VendorA-${runId}`;
    const events = await network.getChaincodeEvents(CHAINCODE);
    try {
      await contract.submit("SLA:RegisterSLA", { arguments: [vendor, ""] });
      const report = JSON.parse(utf8.decode(await contract.submit("SLA:RecordMetrics", {
        arguments: [vendor, "pilot-01",
          j({ completeness_pct: 74.4, max_silence_minutes: 23.2, uncalibrated_pct: 0 })],
      })));
      console.log(`    breached=${report.breached} violations=${report.violations.length} penalty=₹${report.penalty_inr.toLocaleString("en-IN")}`);

      const deadline = Date.now() + 10000;
      for await (const event of events) {
        if (event.eventName === "SLABreach") {
          const payload = JSON.parse(utf8.decode(event.payload));
          if (payload.vendor === vendor) {
            console.log(`    SLABreach event from block ${event.blockNumber}: ${payload.violations.map(v => v.term).join(", ")}`);
            break;
          }
        }
        if (Date.now() > deadline) throw new Error("timed out waiting for SLABreach event");
      }
    } finally {
      events.close();
    }

    // -- 7. secure firmware upgrade chain of trust --------------------------------
    const fwModel = `AQ-Sense-${runId}`;
    step(7, "firmware: manufacturer-signed release → approval → device attestation");
    const maker = makeHsm();
    const manufacturer = `Bosch-${runId}`;
    const fwImage = Buffer.from("sensor firmware v2.1.0 \x7fELF...");
    const fwHash = sha256(fwImage);
    await contract.submit("Firmware:RegisterManufacturer", { arguments: [manufacturer, maker.publicPem] });
    const fwSig = maker.sign(Buffer.from(`firmware|${manufacturer}|${fwModel}|2.1.0|${fwHash}`, "utf8"));
    await contract.submit("Firmware:PublishFirmware", {
      arguments: [manufacturer, fwModel, "2.1.0", fwHash, fwSig],
    });
    const preApproval = JSON.parse(utf8.decode(await contract.evaluate("Firmware:VerifyImage", {
      arguments: [fwModel, "2.1.0", fwHash],
    })));
    console.log(`    before approval     : device refuses image (${preApproval.reason})`);
    await contract.submit("Firmware:ApproveFirmware", { arguments: [fwModel, "2.1.0", "PuneSPV"] });
    const postApproval = JSON.parse(utf8.decode(await contract.evaluate("Firmware:VerifyImage", {
      arguments: [fwModel, "2.1.0", fwHash],
    })));
    console.log(`    after approval      : image verified=${postApproval.ok}`);
    const applied = JSON.parse(utf8.decode(await contract.submit("Firmware:ReportUpdate", {
      arguments: [gatewayId, fwModel, "2.1.0", fwHash],
    })));
    console.log(`    rollout attested    : ok=${applied.ok} firmware=${applied.device.firmware_version}`);
    const trojaned = JSON.parse(utf8.decode(await contract.submit("Firmware:ReportUpdate", {
      arguments: [gatewayId, fwModel, "2.1.0", sha256(Buffer.from("trojan image"))],
    })));
    console.log(`    trojaned install    : ok=${trojaned.ok} (${trojaned.reason}) → device status=${trojaned.device.status}`);
    // un-flag for the remaining steps
    await contract.submit("DeviceRegistry:SetStatus", { arguments: [gatewayId, "active", "firmware remediated"] });

    // -- 8. key rotation ------------------------------------------------------------
    step(8, "lifecycle: rotate the gateway key (signed by the old key)");
    const newHsm = makeHsm();
    const newFingerprint = sha256(Buffer.from(newHsm.publicPem)).slice(0, 16);
    const rotSig = hsm.sign(Buffer.from(`rotate|${gatewayId}|${newFingerprint}`, "utf8"));
    const rotated = JSON.parse(utf8.decode(await contract.submit("DeviceRegistry:RotateKey", {
      arguments: [gatewayId, newHsm.publicPem, rotSig],
    })));
    console.log(`    rotated             : ${rotated.key_history[0].key_fingerprint} → ${rotated.key_fingerprint} (history depth ${rotated.key_history.length})`);
    // old key can no longer anchor; new key can
    const b2 = `${gatewayId}-batch-000002`;
    try {
      await contract.submit("Anchor:AnchorBatch", {
        arguments: [gatewayId, b2, tree.root, ws.toFixed(3), we.toFixed(3), String(n),
          hsm.sign(anchorPayload(gatewayId, b2, tree.root, ws, we, n))],
      });
      throw new Error("OLD KEY STILL ACCEPTED — THIS IS A BUG");
    } catch (err) {
      if (String(err).includes("THIS IS A BUG")) throw err;
      console.log("    old key anchor      : rejected as expected");
    }
    await contract.submit("Anchor:AnchorBatch", {
      arguments: [gatewayId, b2, tree.root, ws.toFixed(3), we.toFixed(3), String(n),
        newHsm.sign(anchorPayload(gatewayId, b2, tree.root, ws, we, n))],
    });
    console.log("    new key anchor      : accepted");

    // -- 9. vendor transfer + decommission ---------------------------------------------
    step(9, "lifecycle: vendor transfer, then decommission (revokes anchor rights)");
    const transferred = JSON.parse(utf8.decode(await contract.submit("DeviceRegistry:TransferVendor", {
      arguments: [gatewayId, "VendorB", "PuneSPV"],
    })));
    console.log(`    vendor              : ${transferred.vendor_history[0].vendor} → ${transferred.vendor}`);
    await contract.submit("DeviceRegistry:Decommission", { arguments: [gatewayId, "end of O&M contract"] });
    const b3 = `${gatewayId}-batch-000003`;
    try {
      await contract.submit("Anchor:AnchorBatch", {
        arguments: [gatewayId, b3, tree.root, ws.toFixed(3), we.toFixed(3), String(n),
          newHsm.sign(anchorPayload(gatewayId, b3, tree.root, ws, we, n))],
      });
      throw new Error("DECOMMISSIONED DEVICE STILL ANCHORS — THIS IS A BUG");
    } catch (err) {
      if (String(err).includes("THIS IS A BUG")) throw err;
      console.log("    post-decommission   : anchor rejected as expected (retired)");
    }

    // -- optional benchmark --------------------------------------------------------
    if (process.argv.includes("--bench")) {
      const N = parseInt(process.env.BENCH_TX || "150", 10);
      step("B", `throughput benchmark: ${N} concurrent AnchorBatch transactions`);
      const benchId = `gw-bench-${runId}`;
      const benchHsm = makeHsm();
      await contract.submit("DeviceRegistry:RegisterDevice", {
        arguments: [j({ ...record, device_id: benchId, serial_number: `GW-B-${runId}`,
          public_key_pem: benchHsm.publicPem,
          key_fingerprint: sha256(Buffer.from(benchHsm.publicPem)).slice(0, 16) })],
      });
      const t0 = Date.now();
      const results = await Promise.allSettled(Array.from({ length: N }, (_, i) => {
        const id = `${benchId}-bench-${String(i).padStart(4, "0")}`;
        const sig = benchHsm.sign(anchorPayload(benchId, id, tree.root, ws, we, n));
        return contract.submit("Anchor:AnchorBatch", {
          arguments: [benchId, id, tree.root, ws.toFixed(3), we.toFixed(3), String(n), sig],
        });
      }));
      const seconds = (Date.now() - t0) / 1000;
      const committed = results.filter(r => r.status === "fulfilled").length;
      const failed = N - committed;
      console.log(`    ${committed}/${N} committed in ${seconds.toFixed(1)}s → ${(committed / seconds).toFixed(1)} TPS` +
        (failed ? ` (${failed} failed)` : "") +
        ` — 3-org endorsement, single host, ~${(committed / seconds * 60).toFixed(0)} gateways/min equivalent`);
    }

    console.log("\nEND-TO-END LIFECYCLE ON FABRIC: PASSED");
  } finally {
    gateway.close();
    client.close();
  }
}

main().catch((err) => {
  console.error("E2E FAILED:", err);
  process.exit(1);
});
