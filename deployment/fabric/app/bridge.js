/*
 * SensorChain Fabric bridge — the edge gateway's door into the shard.
 *
 * A small localhost HTTP service wrapping the Fabric Gateway SDK, so
 * the lightweight Python gateway agent (edge/mqtt_gateway.py) can
 * register itself and submit Merkle anchors without carrying gRPC,
 * protobuf, or Fabric dependencies on the gateway hardware.
 *
 * In production this runs alongside the city's Fabric peer; the edge
 * device speaks plain HTTPS to it. All payload signatures are still
 * produced on the edge device's HSM and verified ON-CHAIN — the bridge
 * is untrusted plumbing: it cannot forge anchors.
 *
 *   POST /register    {record}                    → DeviceRegistry:RegisterDevice
 *   POST /calibrate   {device_id, lab...}         → Calibration:AnchorCertificate
 *   POST /anchor      {gateway_id, batch_id, ...} → Anchor:AnchorBatch
 *   POST /anomaly     {kind, device_id, ...}      → Oracle:ReportAnomaly
 *   POST /verify      {batch_id, leaf_hash, path} → Anchor:VerifyReading
 *   GET  /devices                                 → DeviceRegistry:ListDevices (+calibration)
 *   GET  /device/<id>                             → DeviceRegistry:GetDevice (+calibration)
 *   GET  /anchors                                 → Anchor:ListAnchors
 *   GET  /anchor/<batchId>                        → Anchor:GetAnchor
 *   GET  /anomalies                               → Oracle:ListAnomalies
 *   GET  /sla-breaches                            → SLA:Breaches
 *   GET  /chain                                   → qscc GetChainInfo (height + head hash)
 *   GET  /health
 */

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const grpc = require("@grpc/grpc-js");
const { connect, signers } = require("@hyperledger/fabric-gateway");

const PORT = parseInt(process.env.BRIDGE_PORT || "8801", 10);
const CHANNEL = process.env.CHANNEL || "pune";
const CHAINCODE = "sensorchain";
const MSP_ID = "CitySPVMSP";
const PEER_ENDPOINT = process.env.PEER_ENDPOINT || "localhost:7051";
const PEER_HOST_ALIAS = "peer0.cityspv.sensorchain.local";

const CRYPTO = path.resolve(__dirname, "..", "crypto-config",
  "peerOrganizations", "cityspv.sensorchain.local");
const USER_MSP = path.join(CRYPTO, "users", "User1@cityspv.sensorchain.local", "msp");
const PEER_TLS_CA = path.join(CRYPTO, "peers", PEER_HOST_ALIAS, "tls", "ca.crt");

const utf8 = new TextDecoder();
const firstFile = (dir) => path.join(dir, fs.readdirSync(dir)[0]);

function newGateway() {
  const client = new grpc.Client(PEER_ENDPOINT,
    grpc.credentials.createSsl(fs.readFileSync(PEER_TLS_CA)),
    { "grpc.ssl_target_name_override": PEER_HOST_ALIAS });
  const gateway = connect({
    client,
    identity: { mspId: MSP_ID, credentials: fs.readFileSync(firstFile(path.join(USER_MSP, "signcerts"))) },
    signer: signers.newPrivateKeySigner(
      crypto.createPrivateKey(fs.readFileSync(firstFile(path.join(USER_MSP, "keystore"))))),
    evaluateOptions: () => ({ deadline: Date.now() + 15000 }),
    endorseOptions: () => ({ deadline: Date.now() + 30000 }),
    submitOptions: () => ({ deadline: Date.now() + 15000 }),
    commitStatusOptions: () => ({ deadline: Date.now() + 60000 }),
  });
  return { gateway, client };
}

const { gateway } = newGateway();
const network = gateway.getNetwork(CHANNEL);
const contract = network.getContract(CHAINCODE);
const qscc = network.getContract("qscc");

const evalJson = async (name, args = []) =>
  JSON.parse(utf8.decode(await contract.evaluate(name, { arguments: args })));

async function withCalibration(device) {
  const calibration = await evalJson("Calibration:CalibrationStatus", [device.device_id]);
  const { public_key_pem, ...compact } = device;
  return { ...compact, calibration };
}

const readBody = (req) => new Promise((resolve, reject) => {
  let data = "";
  req.on("data", (chunk) => { data += chunk; });
  req.on("end", () => resolve(data));
  req.on("error", reject);
});

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, { "content-type": "application/json" });
    res.end(JSON.stringify(obj));
  };
  try {
    if (req.method === "GET" && req.url === "/health") {
      return send(200, { ok: true, channel: CHANNEL, chaincode: CHAINCODE });
    }
    if (req.method === "POST" && req.url === "/register") {
      const record = JSON.parse(await readBody(req));
      const result = await contract.submit("DeviceRegistry:RegisterDevice",
        { arguments: [JSON.stringify(record)] });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "POST" && req.url === "/anchor") {
      const a = JSON.parse(await readBody(req));
      const result = await contract.submit("Anchor:AnchorBatch", {
        arguments: [a.gateway_id, a.batch_id, a.merkle_root,
          Number(a.window_start).toFixed(3), Number(a.window_end).toFixed(3),
          String(a.count), a.signature],
      });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "GET" && req.url.startsWith("/anchor/")) {
      const batchId = decodeURIComponent(req.url.slice("/anchor/".length));
      const result = await contract.evaluate("Anchor:GetAnchor", { arguments: [batchId] });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "POST" && req.url === "/calibrate") {
      const c = JSON.parse(await readBody(req));
      const result = await contract.submit("Calibration:AnchorCertificate", {
        arguments: [c.device_id, c.lab_id, c.lab_accreditation, c.result,
          JSON.stringify(c.parameters || {})],
      });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "POST" && req.url === "/anomaly") {
      const anomaly = await readBody(req);
      const result = await contract.submit("Oracle:ReportAnomaly", { arguments: [anomaly] });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "POST" && req.url === "/verify") {
      const v = JSON.parse(await readBody(req));
      const result = await contract.evaluate("Anchor:VerifyReading", {
        arguments: [v.batch_id, v.leaf_hash, JSON.stringify(v.path)],
      });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "GET" && req.url.startsWith("/device/")) {
      const deviceId = decodeURIComponent(req.url.slice("/device/".length));
      const device = await evalJson("DeviceRegistry:GetDevice", [deviceId]);
      return send(200, await withCalibration(device));
    }
    if (req.method === "GET" && (req.url === "/devices" || req.url.startsWith("/devices?"))) {
      const vendor = new URL(req.url, "http://x").searchParams.get("vendor") || "";
      const devices = await evalJson("DeviceRegistry:ListDevices", [vendor]);
      return send(200, await Promise.all(devices.map(withCalibration)));
    }
    if (req.method === "GET" && (req.url === "/anchors" || req.url.startsWith("/anchors?"))) {
      const gatewayId = new URL(req.url, "http://x").searchParams.get("gateway_id") || "";
      return send(200, await evalJson("Anchor:ListAnchors", [gatewayId]));
    }
    if (req.method === "GET" && req.url === "/anomalies") {
      return send(200, await evalJson("Oracle:ListAnomalies"));
    }
    if (req.method === "GET" && (req.url === "/sla-breaches" || req.url.startsWith("/sla-breaches?"))) {
      const vendor = new URL(req.url, "http://x").searchParams.get("vendor") || "";
      return send(200, await evalJson("SLA:Breaches", [vendor]));
    }
    if (req.method === "GET" && (req.url === "/shard-anchors" || req.url.startsWith("/shard-anchors?"))) {
      const shard = new URL(req.url, "http://x").searchParams.get("shard") || "";
      return send(200, await evalJson("Rollup:ShardAnchors", [shard]));
    }
    if (req.method === "POST" && req.url === "/rollup-anchor") {
      const r = JSON.parse(await readBody(req));
      const result = await contract.submit("Rollup:AnchorShardHead", {
        arguments: [r.shard_id, String(r.height), r.head_hash],
      });
      return send(200, JSON.parse(utf8.decode(result)));
    }
    if (req.method === "GET" && req.url === "/chain") {
      const { common } = require("@hyperledger/fabric-protos");
      const bytes = await qscc.evaluate("GetChainInfo", { arguments: [CHANNEL] });
      const info = common.BlockchainInfo.deserializeBinary(bytes);
      return send(200, {
        channel: CHANNEL,
        height: Number(info.getHeight()),
        head_hash: Buffer.from(info.getCurrentblockhash_asU8()).toString("hex"),
      });
    }
    send(404, { error: "unknown endpoint" });
  } catch (err) {
    send(502, { error: String(err.message || err).split("\n")[0] });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`sensorchain bridge listening on 127.0.0.1:${PORT} → ${PEER_ENDPOINT} (${CHANNEL}/${CHAINCODE})`);
});
