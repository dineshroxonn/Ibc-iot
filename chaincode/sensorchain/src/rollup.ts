/*
 * National Rollup contract.
 *
 * Deployed on the national channel. Each city shard's head block hash
 * is periodically anchored here by the rollup service, so cross-city
 * audit and comparison never depend on any single city's operators —
 * and a city cannot silently rewrite its shard without diverging from
 * its anchored heads.
 */

import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";

export interface ShardAnchor {
  shard_id: string;
  height: number;
  head_hash: string;
  anchored_at: number;
  anchored_by?: string;
}

@Info({
  title: "Rollup",
  description: "National rollup: anchors each city shard's head block hash",
})
export class RollupContract extends Contract {
  constructor() {
    super("Rollup");
  }

  @Transaction()
  @Returns("string")
  public async AnchorShardHead(ctx: Context, shardId: string, height: string, headHash: string): Promise<string> {
    const anchor: ShardAnchor = {
      shard_id: shardId,
      height: parseInt(height, 10),
      head_hash: headHash,
      anchored_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
      anchored_by: ctx.clientIdentity.getMSPID(),
    };
    const key = ctx.stub.createCompositeKey("shard", [shardId, String(anchor.height).padStart(12, "0")]);
    await ctx.stub.putState(key, Buffer.from(JSON.stringify(anchor)));
    ctx.stub.setEvent("ShardHeadAnchored", Buffer.from(JSON.stringify(anchor)));
    return JSON.stringify(anchor);
  }

  @Transaction(false)
  @Returns("string")
  public async ShardAnchors(ctx: Context, shardId: string): Promise<string> {
    const iterator = ctx.stub.getStateByPartialCompositeKey("shard", shardId ? [shardId] : []);
    const anchors: ShardAnchor[] = [];
    for await (const kv of iterator) {
      anchors.push(JSON.parse(kv.value.toString()) as ShardAnchor);
    }
    return JSON.stringify(anchors);
  }
}
