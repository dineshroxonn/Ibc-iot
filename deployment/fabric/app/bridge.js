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
 *   POST /register  {record}                      → DeviceRegistry:RegisterDevice
 *   POST /anchor    {gateway_id, batch_id, ...}   → Anchor:AnchorBatch
 *   GET  /anchor/<batchId>                        → Anchor:GetAnchor
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
const contract = gateway.getNetwork(CHANNEL).getContract(CHAINCODE);

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
    send(404, { error: "unknown endpoint" });
  } catch (err) {
    send(502, { error: String(err.message || err).split("\n")[0] });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`sensorchain bridge listening on 127.0.0.1:${PORT} → ${PEER_ENDPOINT} (${CHANNEL}/${CHAINCODE})`);
});
