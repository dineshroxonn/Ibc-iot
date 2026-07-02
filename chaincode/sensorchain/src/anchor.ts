/*
 * Merkle-Root Batch Anchor contract (production chaincode).
 * Mirrors sensorchain/contracts/anchor.py.
 *
 * The gateway signs `gatewayId|batchId|merkleRoot|windowStart|windowEnd|count`
 * with its HSM key (ECDSA P-256 / SHA-256, DER signature). The contract
 * verifies the signature against the public key registered at procurement
 * before accepting the anchor.
 */

import { createVerify } from "crypto";
import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";
import { deviceKey, DeviceRecord } from "./deviceRegistry";

export interface BatchAnchor {
  batch_id: string;
  gateway_id: string;
  merkle_root: string;
  window_start: number;
  window_end: number;
  reading_count: number;
  signature: string;
  anchored_at: number;
}

export function signingPayload(
  gatewayId: string, batchId: string, merkleRoot: string,
  windowStart: number, windowEnd: number, count: number,
): Buffer {
  return Buffer.from(
    `${gatewayId}|${batchId}|${merkleRoot}|${windowStart.toFixed(3)}|${windowEnd.toFixed(3)}|${count}`,
    "utf8",
  );
}

@Info({
  title: "Anchor",
  description: "Merkle-root batch anchoring with gateway signature verification",
})
export class AnchorContract extends Contract {
  constructor() {
    super("Anchor");
  }

  private key(ctx: Context, batchId: string): string {
    return ctx.stub.createCompositeKey("anchor", [batchId]);
  }

  @Transaction()
  @Returns("string")
  public async AnchorBatch(
    ctx: Context, gatewayId: string, batchId: string, merkleRoot: string,
    windowStart: string, windowEnd: string, count: string, signatureHex: string,
  ): Promise<string> {
    const gatewayBytes = await ctx.stub.getState(deviceKey(ctx, gatewayId));
    if (gatewayBytes.length === 0) {
      throw new Error(`anchor rejected: gateway ${gatewayId} is not registered`);
    }
    const gateway = JSON.parse(gatewayBytes.toString()) as DeviceRecord;
    if (gateway.status === "retired") {
      throw new Error(`anchor rejected: gateway ${gatewayId} is retired`);
    }

    const payload = signingPayload(
      gatewayId, batchId, merkleRoot,
      parseFloat(windowStart), parseFloat(windowEnd), parseInt(count, 10),
    );
    const verifier = createVerify("SHA256");
    verifier.update(payload);
    const valid = verifier.verify(gateway.public_key_pem, Buffer.from(signatureHex, "hex"));
    if (!valid) {
      ctx.stub.setEvent("AnchorSignatureInvalid", Buffer.from(JSON.stringify({
        gateway_id: gatewayId, batch_id: batchId, merkle_root: merkleRoot,
      })));
      throw new Error(`anchor rejected: invalid signature from ${gatewayId}`);
    }

    const key = this.key(ctx, batchId);
    const existing = await ctx.stub.getState(key);
    if (existing.length > 0) {
      throw new Error(`batch ${batchId} already anchored`);
    }

    const anchor: BatchAnchor = {
      batch_id: batchId,
      gateway_id: gatewayId,
      merkle_root: merkleRoot,
      window_start: parseFloat(windowStart),
      window_end: parseFloat(windowEnd),
      reading_count: parseInt(count, 10),
      signature: signatureHex,
      anchored_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
    };
    await ctx.stub.putState(key, Buffer.from(JSON.stringify(anchor)));
    return JSON.stringify(anchor);
  }

  @Transaction(false)
  @Returns("string")
  public async ListAnchors(ctx: Context, gatewayId: string): Promise<string> {
    const iterator = ctx.stub.getStateByPartialCompositeKey("anchor", []);
    const anchors: BatchAnchor[] = [];
    for await (const kv of iterator) {
      const anchor = JSON.parse(kv.value.toString()) as BatchAnchor;
      if (!gatewayId || anchor.gateway_id === gatewayId) {
        anchors.push(anchor);
      }
    }
    anchors.sort((a, b) => a.window_start - b.window_start);
    return JSON.stringify(anchors);
  }

  @Transaction(false)
  @Returns("string")
  public async GetAnchor(ctx: Context, batchId: string): Promise<string> {
    const data = await ctx.stub.getState(this.key(ctx, batchId));
    if (data.length === 0) {
      throw new Error(`no anchor for batch ${batchId}`);
    }
    return data.toString();
  }

  /**
   * Citizen verification: recompute the root from a leaf hash and a
   * Merkle path (JSON array of [siblingHashHex, "left"|"right"]) and
   * compare with the anchored root.
   */
  @Transaction(false)
  @Returns("string")
  public async VerifyReading(ctx: Context, batchId: string, leafHash: string, pathJson: string): Promise<string> {
    const { createHash } = await import("crypto");
    const anchor = JSON.parse(await this.GetAnchor(ctx, batchId)) as BatchAnchor;
    let node = leafHash;
    for (const [sibling, side] of JSON.parse(pathJson) as [string, string][]) {
      const pair = side === "left"
        ? Buffer.concat([Buffer.from([1]), Buffer.from(sibling, "hex"), Buffer.from(node, "hex")])
        : Buffer.concat([Buffer.from([1]), Buffer.from(node, "hex"), Buffer.from(sibling, "hex")]);
      node = createHash("sha256").update(pair).digest("hex");
    }
    const verified = node === anchor.merkle_root;
    return JSON.stringify({
      verified,
      computed_root: node,
      onchain_root: anchor.merkle_root,
      ...(verified ? {} : { reason: "Merkle proof does not match on-chain root — data altered after anchoring" }),
    });
  }
}
