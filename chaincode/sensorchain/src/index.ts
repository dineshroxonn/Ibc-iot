import { AnchorContract } from "./anchor";
import { CalibrationContract } from "./calibration";
import { DeviceRegistryContract } from "./deviceRegistry";
import { SLAContract } from "./sla";

export { AnchorContract, CalibrationContract, DeviceRegistryContract, SLAContract };

export const contracts = [
  DeviceRegistryContract,
  CalibrationContract,
  AnchorContract,
  SLAContract,
];
