/*
 * Device Identity Registry contract (production chaincode).
 *
 * Mirrors sensorchain/contracts/device_registry.py. Deployed per city
 * channel; each city shard runs 3 DPoA endorsing peers.
 */

import { createHash, createVerify } from "crypto";
import { Context, Contract, Info, Returns, Transaction } from "fabric-contract-api";

export interface DeviceRecord {
  device_id: string;
  sensor_type: string;
  manufacturer: string;
  serial_number: string;
  bis_certificate: string;
  vendor: string;
  city: string;
  latitude: number;
  longitude: number;
  public_key_pem: string;
  key_fingerprint: string;
  status: string;
  status_reason?: string;
  registered_at: number;
  key_history?: { key_fingerprint: string; public_key_pem: string; retired_at: number }[];
  vendor_history?: { vendor: string; until: number }[];
  decommissioned_at?: number;
}

export function rotationSigningPayload(deviceId: string, newKeyFingerprint: string): Buffer {
  return Buffer.from(`rotate|${deviceId}|${newKeyFingerprint}`, "utf8");
}

const VALID_STATUSES = new Set(["active", "maintenance", "retired", "flagged"]);

export function deviceKey(ctx: Context, deviceId: string): string {
  return ctx.stub.createCompositeKey("device", [deviceId]);
}

@Info({
  title: "DeviceRegistry",
  description: "On-chain IoT device identity with BIS certification enforcement",
})
export class DeviceRegistryContract extends Contract {
  constructor() {
    super("DeviceRegistry");
  }

  @Transaction()
  @Returns("string")
  public async RegisterDevice(ctx: Context, recordJson: string): Promise<string> {
    const record = JSON.parse(recordJson) as DeviceRecord;
    const required: (keyof DeviceRecord)[] = [
      "device_id", "sensor_type", "manufacturer", "serial_number",
      "bis_certificate", "vendor", "city", "latitude", "longitude",
      "public_key_pem", "key_fingerprint",
    ];
    for (const field of required) {
      if (record[field] === undefined || record[field] === null) {
        throw new Error(`device record missing field: ${field}`);
      }
    }
    if (!record.bis_certificate) {
      throw new Error(`device ${record.device_id} has no BIS IS 17927 certificate — registration refused`);
    }
    const key = deviceKey(ctx, record.device_id);
    const existing = await ctx.stub.getState(key);
    if (existing.length > 0) {
      throw new Error(`device ${record.device_id} already registered`);
    }
    record.status = "active";
    record.registered_at = ctx.stub.getTxTimestamp().seconds.toNumber();

    await ctx.stub.putState(key, Buffer.from(JSON.stringify(record)));
    ctx.stub.setEvent("DeviceRegistered", Buffer.from(JSON.stringify({
      device_id: record.device_id,
      vendor: record.vendor,
      key_fingerprint: record.key_fingerprint,
    })));
    return JSON.stringify(record);
  }

  @Transaction(false)
  @Returns("string")
  public async GetDevice(ctx: Context, deviceId: string): Promise<string> {
    const data = await ctx.stub.getState(deviceKey(ctx, deviceId));
    if (data.length === 0) {
      throw new Error(`unknown device ${deviceId}`);
    }
    return data.toString();
  }

  @Transaction()
  @Returns("string")
  public async SetStatus(ctx: Context, deviceId: string, status: string, reason: string): Promise<string> {
    if (!VALID_STATUSES.has(status)) {
      throw new Error(`invalid status ${status}`);
    }
    const record = JSON.parse(await this.GetDevice(ctx, deviceId)) as DeviceRecord;
    record.status = status;
    record.status_reason = reason;
    await ctx.stub.putState(deviceKey(ctx, deviceId), Buffer.from(JSON.stringify(record)));
    ctx.stub.setEvent("DeviceStatusChanged", Buffer.from(JSON.stringify({
      device_id: deviceId, status, reason,
    })));
    return JSON.stringify(record);
  }

  /**
   * Rotate the device key. Must be authorized by a signature from the
   * CURRENT key over `rotate|deviceId|newKeyFingerprint` — proof of
   * possession. The old key is kept in on-chain history so anchors made
   * under it remain attributable.
   */
  @Transaction()
  @Returns("string")
  public async RotateKey(ctx: Context, deviceId: string, newPublicKeyPem: string, signatureHex: string): Promise<string> {
    const record = JSON.parse(await this.GetDevice(ctx, deviceId)) as DeviceRecord;
    if (record.status === "retired") {
      throw new Error(`cannot rotate key of retired device ${deviceId}`);
    }
    const newFingerprint = createHash("sha256").update(newPublicKeyPem).digest("hex").slice(0, 16);
    const verifier = createVerify("SHA256");
    verifier.update(rotationSigningPayload(deviceId, newFingerprint));
    if (!verifier.verify(record.public_key_pem, Buffer.from(signatureHex, "hex"))) {
      ctx.stub.setEvent("KeyRotationRejected", Buffer.from(JSON.stringify({
        device_id: deviceId, attempted_fingerprint: newFingerprint,
      })));
      throw new Error(`key rotation for ${deviceId} rejected: not signed by the current device key`);
    }
    const history = record.key_history ?? [];
    history.push({
      key_fingerprint: record.key_fingerprint,
      public_key_pem: record.public_key_pem,
      retired_at: ctx.stub.getTxTimestamp().seconds.toNumber(),
    });
    record.public_key_pem = newPublicKeyPem;
    record.key_fingerprint = newFingerprint;
    record.key_history = history;
    await ctx.stub.putState(deviceKey(ctx, deviceId), Buffer.from(JSON.stringify(record)));
    ctx.stub.setEvent("DeviceKeyRotated", Buffer.from(JSON.stringify({
      device_id: deviceId,
      old_fingerprint: history[history.length - 1].key_fingerprint,
      new_fingerprint: newFingerprint,
    })));
    return JSON.stringify(record);
  }

  @Transaction()
  @Returns("string")
  public async TransferVendor(ctx: Context, deviceId: string, newVendor: string, authorizedBy: string): Promise<string> {
    const record = JSON.parse(await this.GetDevice(ctx, deviceId)) as DeviceRecord;
    const history = record.vendor_history ?? [];
    history.push({ vendor: record.vendor, until: ctx.stub.getTxTimestamp().seconds.toNumber() });
    const fromVendor = record.vendor;
    record.vendor = newVendor;
    record.vendor_history = history;
    await ctx.stub.putState(deviceKey(ctx, deviceId), Buffer.from(JSON.stringify(record)));
    ctx.stub.setEvent("DeviceVendorTransferred", Buffer.from(JSON.stringify({
      device_id: deviceId, from_vendor: fromVendor, to_vendor: newVendor, authorized_by: authorizedBy,
    })));
    return JSON.stringify(record);
  }

  /**
   * End of life: sets the device to retired, which revokes its anchor
   * rights (the Anchor contract refuses retired devices). Irreversible.
   */
  @Transaction()
  @Returns("string")
  public async Decommission(ctx: Context, deviceId: string, reason: string): Promise<string> {
    const record = JSON.parse(await this.GetDevice(ctx, deviceId)) as DeviceRecord;
    record.status = "retired";
    record.status_reason = reason;
    record.decommissioned_at = ctx.stub.getTxTimestamp().seconds.toNumber();
    await ctx.stub.putState(deviceKey(ctx, deviceId), Buffer.from(JSON.stringify(record)));
    ctx.stub.setEvent("DeviceDecommissioned", Buffer.from(JSON.stringify({
      device_id: deviceId, reason,
    })));
    return JSON.stringify(record);
  }

  @Transaction(false)
  @Returns("string")
  public async ListDevices(ctx: Context, vendor: string): Promise<string> {
    const iterator = ctx.stub.getStateByPartialCompositeKey("device", []);
    const devices: DeviceRecord[] = [];
    for await (const kv of iterator) {
      const record = JSON.parse(kv.value.toString()) as DeviceRecord;
      if (!vendor || record.vendor === vendor) {
        devices.push(record);
      }
    }
    return JSON.stringify(devices);
  }
}
