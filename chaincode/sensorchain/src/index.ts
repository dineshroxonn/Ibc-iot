import { AnchorContract } from "./anchor";
import { CalibrationContract } from "./calibration";
import { DeviceRegistryContract } from "./deviceRegistry";
import { FirmwareContract } from "./firmware";
import { SLAContract } from "./sla";

export { AnchorContract, CalibrationContract, DeviceRegistryContract, FirmwareContract, SLAContract };

export const contracts = [
  DeviceRegistryContract,
  CalibrationContract,
  AnchorContract,
  FirmwareContract,
  SLAContract,
];
