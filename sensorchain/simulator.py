"""City sensor fleet simulator.

Generates realistic multi-modal sensor traffic (AQI, water quality,
traffic counts) for a pilot city, with injectable fault scenarios used
to exercise the oracle and SLA contracts:

  under_report — a vendor's AQI sensor starts reporting ~40% of the
                 true value (the pollution under-reporting case from
                 the problem statement)
  spike        — implausible step jump (tampering / hardware fault)
  silence      — sensor goes dark mid-run
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

BASELINES = {"aqi": 120.0, "water_quality": 7.1, "traffic": 35.0}
NOISE = {"aqi": 6.0, "water_quality": 0.08, "traffic": 4.0}
DIURNAL_AMPLITUDE = {"aqi": 30.0, "water_quality": 0.05, "traffic": 20.0}


@dataclass
class SimulatedSensor:
    device_id: str
    sensor_type: str
    site_offset: float = 0.0          # per-site level shift
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))
    # fault injection
    under_report_from: float | None = None
    under_report_factor: float = 0.4
    spike_at: float | None = None
    spike_magnitude: float = 4.0      # multiples of baseline added at the spike
    silent_from: float | None = None

    def value_at(self, t: float) -> float | None:
        """Reading at simulation time t (seconds); None while silent."""
        if self.silent_from is not None and t >= self.silent_from:
            return None
        base = BASELINES[self.sensor_type] + self.site_offset
        diurnal = DIURNAL_AMPLITUDE[self.sensor_type] * math.sin(2 * math.pi * t / 86_400)
        value = base + diurnal + self.rng.normal(0.0, NOISE[self.sensor_type])
        if self.spike_at is not None and t >= self.spike_at:
            value += BASELINES[self.sensor_type] * self.spike_magnitude
        if self.under_report_from is not None and t >= self.under_report_from:
            value *= self.under_report_factor
        return max(value, 0.0)

    def normal_series(self, n: int, step: float = 60.0, seed: int = 99) -> np.ndarray:
        """Fault-free history used to train the oracle's autoencoder."""
        rng = np.random.default_rng(seed)
        base = BASELINES[self.sensor_type] + self.site_offset
        t = np.arange(n) * step
        return (base
                + DIURNAL_AMPLITUDE[self.sensor_type] * np.sin(2 * np.pi * t / 86_400)
                + rng.normal(0.0, NOISE[self.sensor_type], n))


class CitySimulator:
    """Drives a fleet of SimulatedSensors on a fixed reporting interval."""

    def __init__(self, sensors: list[SimulatedSensor], start_time: float = 1_750_000_000.0,
                 interval_s: float = 12.0):
        self.sensors = sensors
        self.start_time = start_time
        self.interval_s = interval_s

    def run(self, duration_s: float):
        """Yield readings in timestamp order across the whole fleet."""
        steps = int(duration_s // self.interval_s)
        for step in range(steps):
            sim_t = step * self.interval_s
            timestamp = self.start_time + sim_t
            for sensor in self.sensors:
                value = sensor.value_at(sim_t)
                if value is None:
                    continue
                # timestamps as int and values as fixed-decimal strings, so the
                # canonical JSON (and therefore the leaf hash) is identical in
                # Python and in the portal's in-browser JavaScript verifier
                yield {
                    "device_id": sensor.device_id,
                    "sensor_type": sensor.sensor_type,
                    "timestamp": int(timestamp),
                    "value": f"{value:.3f}",
                    "unit": {"aqi": "AQI", "water_quality": "pH", "traffic": "veh/min"}[sensor.sensor_type],
                }
