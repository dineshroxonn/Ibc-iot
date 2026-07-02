/*
 * Anomaly Oracle contract.
 *
 * The AI oracle runs OFF-chain (it needs the raw readings, which never
 * go on-chain) and anchors its findings here, making the detection
 * record itself tamper-evident and auditable. The reporting client's
 * MSP is recorded with each finding; in production the endorsement
 * policy for this contract restricts reporting to the designated
 * oracle identity.
 */

import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";

export interface Anomaly {
  kind: string;        // temporal | silence | spatial | cross_modal
  device_id: string;
  timestamp: number;
  detail: string;
  score: number;
  severity: string;    // warning | critical
  reported_by?: string;
}

@Info({
  title: "Oracle",
  description: "Tamper-evident anchor for AI anomaly-detection findings",
})
export class OracleContract extends Contract {
  constructor() {
    super("Oracle");
  }

  @Transaction()
  @Returns("string")
  public async ReportAnomaly(ctx: Context, anomalyJson: string): Promise<string> {
    const anomaly = JSON.parse(anomalyJson) as Anomaly;
    for (const field of ["kind", "device_id", "timestamp", "detail", "severity"] as const) {
      if (anomaly[field] === undefined || anomaly[field] === null) {
        throw new Error(`anomaly missing field: ${field}`);
      }
    }
    anomaly.reported_by = ctx.clientIdentity.getMSPID();
    const key = ctx.stub.createCompositeKey("anomaly", [
      String(anomaly.timestamp).padStart(14, "0"),
      anomaly.kind,
      ctx.stub.getTxID().slice(0, 12),
    ]);
    await ctx.stub.putState(key, Buffer.from(JSON.stringify(anomaly)));
    ctx.stub.setEvent("AnomalyDetected", Buffer.from(JSON.stringify(anomaly)));
    return JSON.stringify(anomaly);
  }

  @Transaction(false)
  @Returns("string")
  public async ListAnomalies(ctx: Context): Promise<string> {
    const iterator = ctx.stub.getStateByPartialCompositeKey("anomaly", []);
    const anomalies: Anomaly[] = [];
    for await (const kv of iterator) {
      anomalies.push(JSON.parse(kv.value.toString()) as Anomaly);
    }
    return JSON.stringify(anomalies);
  }
}
