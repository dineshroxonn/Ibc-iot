/*
 * Vendor SLA Enforcement contract (production chaincode).
 * Mirrors sensorchain/contracts/sla.py.
 */

import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";

export interface SLATerms {
  min_completeness_pct: number;
  max_silence_minutes: number;
  max_uncalibrated_pct: number;
  penalty_per_breach_inr: number;
}

const DEFAULT_TERMS: SLATerms = {
  min_completeness_pct: 95.0,
  max_silence_minutes: 15.0,
  max_uncalibrated_pct: 0.0,
  penalty_per_breach_inr: 50000,
};

export interface SLAViolation {
  term: string;
  required: number;
  observed: number;
}

@Info({
  title: "SLA",
  description: "System-integrator SLA with automatic breach events and penalties",
})
export class SLAContract extends Contract {
  constructor() {
    super("SLA");
  }

  @Transaction()
  @Returns("string")
  public async RegisterSLA(ctx: Context, vendor: string, termsJson: string): Promise<string> {
    const sla = {
      vendor,
      terms: { ...DEFAULT_TERMS, ...(termsJson ? JSON.parse(termsJson) : {}) },
      registered_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
      active: true,
    };
    await ctx.stub.putState(
      ctx.stub.createCompositeKey("sla", [vendor]),
      Buffer.from(JSON.stringify(sla)),
    );
    ctx.stub.setEvent("SLARegistered", Buffer.from(JSON.stringify({ vendor, terms: sla.terms })));
    return JSON.stringify(sla);
  }

  @Transaction()
  @Returns("string")
  public async RecordMetrics(ctx: Context, vendor: string, period: string, metricsJson: string): Promise<string> {
    const slaBytes = await ctx.stub.getState(ctx.stub.createCompositeKey("sla", [vendor]));
    if (slaBytes.length === 0) {
      throw new Error(`no SLA registered for vendor ${vendor}`);
    }
    const terms = (JSON.parse(slaBytes.toString()) as { terms: SLATerms }).terms;
    const metrics = JSON.parse(metricsJson) as Record<string, number>;

    const violations: SLAViolation[] = [];
    if ((metrics.completeness_pct ?? 100.0) < terms.min_completeness_pct) {
      violations.push({
        term: "min_completeness_pct",
        required: terms.min_completeness_pct,
        observed: metrics.completeness_pct,
      });
    }
    if ((metrics.max_silence_minutes ?? 0.0) > terms.max_silence_minutes) {
      violations.push({
        term: "max_silence_minutes",
        required: terms.max_silence_minutes,
        observed: metrics.max_silence_minutes,
      });
    }
    if ((metrics.uncalibrated_pct ?? 0.0) > terms.max_uncalibrated_pct) {
      violations.push({
        term: "max_uncalibrated_pct",
        required: terms.max_uncalibrated_pct,
        observed: metrics.uncalibrated_pct,
      });
    }

    const report = {
      vendor,
      period,
      metrics,
      violations,
      breached: violations.length > 0,
      penalty_inr: terms.penalty_per_breach_inr * violations.length,
      recorded_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
    };
    await ctx.stub.putState(
      ctx.stub.createCompositeKey("slareport", [vendor, period]),
      Buffer.from(JSON.stringify(report)),
    );
    if (violations.length > 0) {
      ctx.stub.setEvent("SLABreach", Buffer.from(JSON.stringify({
        vendor, period, violations, penalty_inr: report.penalty_inr,
      })));
    }
    return JSON.stringify(report);
  }

  @Transaction(false)
  @Returns("string")
  public async Breaches(ctx: Context, vendor: string): Promise<string> {
    const iterator = ctx.stub.getStateByPartialCompositeKey("slareport", vendor ? [vendor] : []);
    const breaches: unknown[] = [];
    for await (const kv of iterator) {
      const report = JSON.parse(kv.value.toString());
      if (report.breached) {
        breaches.push(report);
      }
    }
    return JSON.stringify(breaches);
  }
}
