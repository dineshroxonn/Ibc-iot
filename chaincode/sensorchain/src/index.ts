import { AnchorContract } from "./anchor";
import { CalibrationContract } from "./calibration";
import { DeviceRegistryContract } from "./deviceRegistry";
import { FirmwareContract } from "./firmware";
import { OracleContract } from "./oracle";
import { RollupContract } from "./rollup";
import { SLAContract } from "./sla";

export {
  AnchorContract, CalibrationContract, DeviceRegistryContract,
  FirmwareContract, OracleContract, RollupContract, SLAContract,
};

export const contracts = [
  DeviceRegistryContract,
  CalibrationContract,
  AnchorContract,
  FirmwareContract,
  OracleContract,
  RollupContract,
  SLAContract,
];
