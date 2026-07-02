/*
 * Secure Firmware Upgrade contract (production chaincode).
 * Mirrors sensorchain/contracts/firmware.py.
 *
 * Chain of trust: manufacturer signing key registered on-chain →
 * releases published with a verified ECDSA signature → city SPV
 * approval gate → device-side hash verification before flashing →
 * post-flash attestation with auto-flagging on mismatch.
 */

import { createHash, createVerify } from "crypto";
import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";
import { deviceKey, DeviceRecord } from "./deviceRegistry";

export interface FirmwareRelease {
  manufacturer: string;
  model: string;
  version: string;
  firmware_hash: string;
  signature: string;
  approved: boolean;
  approved_by?: string;
  published_at: number;
}

export function firmwareSigningPayload(
  manufacturer: string, model: string, version: string, firmwareHash: string,
): Buffer {
  return Buffer.from(`firmware|${manufacturer}|${model}|${version}|${firmwareHash}`, "utf8");
}

@Info({
  title: "Firmware",
  description: "Manufacturer-signed firmware releases with approval gate and rollout attestation",
})
export class FirmwareContract extends Contract {
  constructor() {
    super("Firmware");
  }

  private makerKey(ctx: Context, name: string): string {
    return ctx.stub.createCompositeKey("manufacturer", [name]);
  }

  private releaseKey(ctx: Context, model: string, version: string): string {
    return ctx.stub.createCompositeKey("firmware", [model, version]);
  }

  @Transaction()
  @Returns("string")
  public async RegisterManufacturer(ctx: Context, name: string, publicKeyPem: string): Promise<string> {
    const key = this.makerKey(ctx, name);
    const existing = await ctx.stub.getState(key);
    if (existing.length > 0) {
      throw new Error(`manufacturer ${name} already registered`);
    }
    const record = {
      name,
      public_key_pem: publicKeyPem,
      key_fingerprint: createHash("sha256").update(publicKeyPem).digest("hex").slice(0, 16),
      registered_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
    };
    await ctx.stub.putState(key, Buffer.from(JSON.stringify(record)));
    ctx.stub.setEvent("ManufacturerRegistered", Buffer.from(JSON.stringify({
      name, key_fingerprint: record.key_fingerprint,
    })));
    return JSON.stringify(record);
  }

  @Transaction()
  @Returns("string")
  public async PublishFirmware(
    ctx: Context, manufacturer: string, model: string,
    version: string, firmwareHash: string, signatureHex: string,
  ): Promise<string> {
    const makerBytes = await ctx.stub.getState(this.makerKey(ctx, manufacturer));
    if (makerBytes.length === 0) {
      throw new Error(`unknown manufacturer ${manufacturer}`);
    }
    const maker = JSON.parse(makerBytes.toString()) as { public_key_pem: string };
    const verifier = createVerify("SHA256");
    verifier.update(firmwareSigningPayload(manufacturer, model, version, firmwareHash));
    if (!verifier.verify(maker.public_key_pem, Buffer.from(signatureHex, "hex"))) {
      ctx.stub.setEvent("FirmwareSignatureInvalid", Buffer.from(JSON.stringify({
        manufacturer, model, version,
      })));
      throw new Error(
        `firmware ${model} ${version} rejected: signature does not match ${manufacturer}'s registered signing key`);
    }
    const key = this.releaseKey(ctx, model, version);
    const existing = await ctx.stub.getState(key);
    if (existing.length > 0) {
      throw new Error(`firmware ${model} ${version} already published`);
    }
    const release: FirmwareRelease = {
      manufacturer, model, version,
      firmware_hash: firmwareHash,
      signature: signatureHex,
      approved: false,
      published_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
    };
    await ctx.stub.putState(key, Buffer.from(JSON.stringify(release)));
    ctx.stub.setEvent("FirmwarePublished", Buffer.from(JSON.stringify({
      manufacturer, model, version, firmware_hash: firmwareHash,
    })));
    return JSON.stringify(release);
  }

  @Transaction()
  @Returns("string")
  public async ApproveFirmware(ctx: Context, model: string, version: string, approver: string): Promise<string> {
    const key = this.releaseKey(ctx, model, version);
    const bytes = await ctx.stub.getState(key);
    if (bytes.length === 0) {
      throw new Error(`no published firmware ${model} ${version}`);
    }
    const release = JSON.parse(bytes.toString()) as FirmwareRelease;
    release.approved = true;
    release.approved_by = approver;
    await ctx.stub.putState(key, Buffer.from(JSON.stringify(release)));
    ctx.stub.setEvent("FirmwareApproved", Buffer.from(JSON.stringify({ model, version, approved_by: approver })));
    return JSON.stringify(release);
  }

  @Transaction(false)
  @Returns("string")
  public async VerifyImage(ctx: Context, model: string, version: string, imageHash: string): Promise<string> {
    const bytes = await ctx.stub.getState(this.releaseKey(ctx, model, version));
    if (bytes.length === 0) {
      return JSON.stringify({ ok: false, reason: "unknown_release" });
    }
    const release = JSON.parse(bytes.toString()) as FirmwareRelease;
    if (!release.approved) {
      return JSON.stringify({ ok: false, reason: "not_approved_for_rollout" });
    }
    if (release.firmware_hash !== imageHash) {
      return JSON.stringify({
        ok: false, reason: "image_hash_mismatch",
        expected: release.firmware_hash, got: imageHash,
      });
    }
    return JSON.stringify({ ok: true, release });
  }

  @Transaction()
  @Returns("string")
  public async ReportUpdate(
    ctx: Context, deviceId: string, model: string, version: string, installedHash: string,
  ): Promise<string> {
    const devKey = deviceKey(ctx, deviceId);
    const devBytes = await ctx.stub.getState(devKey);
    if (devBytes.length === 0) {
      throw new Error(`unknown device ${deviceId}`);
    }
    const relBytes = await ctx.stub.getState(this.releaseKey(ctx, model, version));
    if (relBytes.length === 0) {
      throw new Error(`no published firmware ${model} ${version}`);
    }
    const device = JSON.parse(devBytes.toString()) as DeviceRecord & Record<string, unknown>;
    const release = JSON.parse(relBytes.toString()) as FirmwareRelease;

    if (release.approved && installedHash === release.firmware_hash) {
      device.firmware_model = model;
      device.firmware_version = version;
      await ctx.stub.putState(devKey, Buffer.from(JSON.stringify(device)));
      ctx.stub.setEvent("FirmwareUpdateApplied", Buffer.from(JSON.stringify({
        device_id: deviceId, model, version,
      })));
      return JSON.stringify({ ok: true, device });
    }

    const reason = !release.approved ? "unapproved_release" : "installed_hash_mismatch";
    device.status = "flagged";
    device.status_reason = `firmware violation: ${reason}`;
    await ctx.stub.putState(devKey, Buffer.from(JSON.stringify(device)));
    ctx.stub.setEvent("FirmwareHashMismatch", Buffer.from(JSON.stringify({
      device_id: deviceId, model, version, reason,
      expected: release.firmware_hash, got: installedHash,
    })));
    return JSON.stringify({ ok: false, reason, device });
  }
}
