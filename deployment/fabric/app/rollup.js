/*
 * SensorChain national rollup service.
 *
 * Periodically reads each city shard's chain info (height + current
 * block hash, via qscc) and anchors it on the national channel through
 * the Rollup contract. Cross-city audit then never depends on any
 * single city's operators: a shard that rewrites history diverges
 * from its own anchored heads.
 *
 *   node rollup.js --shards pune,ahmedabad --rollup national --rounds 3 --interval 15
 */

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const grpc = require("@grpc/grpc-js");
const { connect, signers } = require("@hyperledger/fabric-gateway");
const { common } = require("@hyperledger/fabric-protos");

const MSP_ID = "CitySPVMSP";
const PEER_ENDPOINT = process.env.PEER_ENDPOINT || "localhost:7051";
const PEER_HOST_ALIAS = "peer0.cityspv.sensorchain.local";
const CRYPTO = path.resolve(__dirname, "..", "crypto-config",
  "peerOrganizations", "cityspv.sensorchain.local");
const USER_MSP = path.join(CRYPTO, "users", "User1@cityspv.sensorchain.local", "msp");
const PEER_TLS_CA = path.join(CRYPTO, "peers", PEER_HOST_ALIAS, "tls", "ca.crt");

const utf8 = new TextDecoder();
const firstFile = (dir) => path.join(dir, fs.readdirSync(dir)[0]);

function argValue(flag, fallback) {
  const index = process.argv.indexOf(flag);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

async function main() {
  const shards = argValue("--shards", "pune,ahmedabad").split(",");
  const rollupChannel = argValue("--rollup", "national");
  const rounds = parseInt(argValue("--rounds", "3"), 10);
  const intervalS = parseFloat(argValue("--interval", "15"));

  const client = new grpc.Client(PEER_ENDPOINT,
    grpc.credentials.createSsl(fs.readFileSync(PEER_TLS_CA)),
    { "grpc.ssl_target_name_override": PEER_HOST_ALIAS });
  const gateway = connect({
    client,
    identity: { mspId: MSP_ID, credentials: fs.readFileSync(firstFile(path.join(USER_MSP, "signcerts"))) },
    signer: signers.newPrivateKeySigner(
      crypto.createPrivateKey(fs.readFileSync(firstFile(path.join(USER_MSP, "keystore"))))),
  });

  const rollup = gateway.getNetwork(rollupChannel).getContract("sensorchain");
  console.log(`[rollup] anchoring heads of [${shards.join(", ")}] onto '${rollupChannel}' ` +
    `every ${intervalS}s × ${rounds} rounds`);

  try {
    for (let round = 1; round <= rounds; round++) {
      for (const shard of shards) {
        const qscc = gateway.getNetwork(shard).getContract("qscc");
        const info = common.BlockchainInfo.deserializeBinary(
          await qscc.evaluate("GetChainInfo", { arguments: [shard] }));
        const height = Number(info.getHeight());
        const headHash = Buffer.from(info.getCurrentblockhash_asU8()).toString("hex");
        const anchored = JSON.parse(utf8.decode(await rollup.submit("Rollup:AnchorShardHead", {
          arguments: [shard, String(height), headHash],
        })));
        console.log(`[rollup] round ${round}: ${shard} height=${height} ` +
          `head=${headHash.slice(0, 16)}… anchored by ${anchored.anchored_by}`);
      }
      if (round < rounds) await new Promise(r => setTimeout(r, intervalS * 1000));
    }

    console.log("\n[rollup] anchors now on the national channel:");
    for (const shard of shards) {
      const anchors = JSON.parse(utf8.decode(
        await rollup.evaluate("Rollup:ShardAnchors", { arguments: [shard] })));
      const latest = anchors[anchors.length - 1];
      console.log(`[rollup]   ${shard}: ${anchors.length} anchors, ` +
        `latest height=${latest.height} head=${latest.head_hash.slice(0, 16)}…`);
    }
  } finally {
    gateway.close();
    client.close();
  }
}

main().catch((err) => { console.error("[rollup] FAILED:", err); process.exit(1); });
