/*
 * Calibration Certificate Anchor contract (production chaincode).
 * Mirrors sensorchain/contracts/calibration.py.
 */

import { createHash } from "crypto";
import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";
import { deviceKey } from "./deviceRegistry";

const VALIDITY_SECONDS = 180 * 24 * 3600; // 180-day calibration cycle

export interface CalibrationCertificate {
  device_id: string;
  lab_id: string;
  lab_accreditation: string;
  result: string; // "pass" | "fail"
  parameters: Record<string, unknown>;
  issued_at: number;
  expires_at: number;
  certificate_hash?: string;
}

@Info({
  title: "Calibration",
  description: "Anchors lab-signed calibration certificates; auto-flags uncalibrated devices",
})
export class CalibrationContract extends Contract {
  constructor() {
    super("Calibration");
  }

  private key(ctx: Context, deviceId: string, issuedAt: number): string {
    return ctx.stub.createCompositeKey("calibration", [deviceId, issuedAt.toFixed(6).padStart(18, "0")]);
  }

  @Transaction()
  @Returns("string")
  public async AnchorCertificate(
    ctx: Context, deviceId: string, labId: string,
    labAccreditation: string, result: string, parametersJson: string,
  ): Promise<string> {
    const device = await ctx.stub.getState(deviceKey(ctx, deviceId));
    if (device.length === 0) {
      throw new Error(`cannot calibrate unregistered device ${deviceId}`);
    }
    const issuedAt = ctx.stub.getTxTimestamp().seconds.toNumber();
    const certificate: CalibrationCertificate = {
      device_id: deviceId,
      lab_id: labId,
      lab_accreditation: labAccreditation,
      result,
      parameters: parametersJson ? JSON.parse(parametersJson) : {},
      issued_at: issuedAt,
      expires_at: issuedAt + VALIDITY_SECONDS,
    };
    certificate.certificate_hash = createHash("sha256")
      .update(JSON.stringify(certificate))
      .digest("hex");
    await ctx.stub.putState(this.key(ctx, deviceId, issuedAt), Buffer.from(JSON.stringify(certificate)));
    ctx.stub.setEvent("CalibrationAnchored", Buffer.from(JSON.stringify({
      device_id: deviceId, lab_id: labId, result,
      certificate_hash: certificate.certificate_hash,
    })));
    return JSON.stringify(certificate);
  }

  @Transaction(false)
  @Returns("string")
  public async CalibrationStatus(ctx: Context, deviceId: string): Promise<string> {
    const iterator = ctx.stub.getStateByPartialCompositeKey("calibration", [deviceId]);
    let latest: CalibrationCertificate | null = null;
    for await (const kv of iterator) {
      const certificate = JSON.parse(kv.value.toString()) as CalibrationCertificate;
      if (latest === null || certificate.issued_at > latest.issued_at) {
        latest = certificate;
      }
    }
    const now = ctx.stub.getTxTimestamp().seconds.toNumber();
    if (latest === null) {
      return JSON.stringify({ device_id: deviceId, calibrated: false, reason: "never_calibrated" });
    }
    if (latest.result !== "pass") {
      return JSON.stringify({
        device_id: deviceId, calibrated: false, reason: "last_calibration_failed",
        certificate_hash: latest.certificate_hash,
      });
    }
    if (now > latest.expires_at) {
      return JSON.stringify({
        device_id: deviceId, calibrated: false, reason: "calibration_expired",
        expired_at: latest.expires_at, certificate_hash: latest.certificate_hash,
      });
    }
    return JSON.stringify({
      device_id: deviceId, calibrated: true,
      expires_at: latest.expires_at, certificate_hash: latest.certificate_hash,
    });
  }

  @Transaction()
  @Returns("string")
  public async FlagUncalibrated(ctx: Context, deviceIdsJson: string): Promise<string> {
    const deviceIds = JSON.parse(deviceIdsJson) as string[];
    const flagged: unknown[] = [];
    for (const deviceId of deviceIds) {
      const status = JSON.parse(await this.CalibrationStatus(ctx, deviceId));
      if (!status.calibrated) {
        ctx.stub.setEvent("UncalibratedDevice", Buffer.from(JSON.stringify(status)));
        flagged.push(status);
      }
    }
    return JSON.stringify(flagged);
  }
}
