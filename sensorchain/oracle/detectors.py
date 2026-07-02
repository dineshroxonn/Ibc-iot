"""Multi-Level AI Anomaly Detection Oracle.

Three detection levels from the proposal, run over anchored batches:

  temporal    — per-device: autoencoder reconstruction error over a
                sliding window, plus sensor-silence detection
  spatial     — co-located sensor groups: robust divergence of one
                device from its neighbours' consensus (median/MAD)
  cross-modal — consistency between sensor modalities at one site
                (e.g. vehicular PM2.5 spike while traffic count ~ 0)

Findings are anchored on-chain as AnomalyDetected events so that the
detection record itself is tamper-evident and auditable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from ..ledger import Ledger
from .autoencoder import Autoencoder


@dataclass
class Anomaly:
    kind: str            # "temporal" | "silence" | "spatial" | "cross_modal"
    device_id: str
    timestamp: float
    detail: str
    score: float
    severity: str        # "warning" | "critical"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "device_id": self.device_id, "timestamp": self.timestamp,
            "detail": self.detail, "score": round(self.score, 4), "severity": self.severity,
        }


@dataclass
class _DeviceState:
    model: Autoencoder | None = None
    history: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, value)


class AnomalyOracle:
    def __init__(
        self,
        ledger: Ledger,
        window: int = 12,
        silence_threshold_s: float = 300.0,
        spatial_z_threshold: float = 5.0,
        dedup_window_s: float = 600.0,
    ):
        self.ledger = ledger
        self.window = window
        self.silence_threshold_s = silence_threshold_s
        self.spatial_z_threshold = spatial_z_threshold
        self.dedup_window_s = dedup_window_s
        self._devices: dict[str, _DeviceState] = defaultdict(_DeviceState)
        # site -> sensor_type -> device_ids (built from the device registry)
        self._sites: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.anomalies: list[Anomaly] = []
        # (kind, device_id) -> last reported timestamp, so a persistent fault
        # is reported once per dedup window instead of once per batch
        self._last_reported: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def load_registry(self) -> None:
        """Group devices into co-location sites from the prototype ledger."""
        self.load_devices([device for _, device in self.ledger.query_by_prefix("device~")])

    def load_devices(self, devices: list[dict]) -> None:
        """Group device records into co-location sites by rounded GPS cell.

        Accepts records from any registry source — the prototype ledger
        or the Fabric bridge's /devices endpoint (same schema)."""
        self._sites.clear()
        for device in devices:
            site = f"{round(device['latitude'], 3)}:{round(device['longitude'], 3)}"
            self._sites[site][device["sensor_type"]].append(device["device_id"])

    def train(self, device_id: str, normal_series: np.ndarray) -> float:
        """Train the per-device autoencoder on known-normal history."""
        model = Autoencoder(window=self.window)
        threshold = model.fit(normal_series)
        self._devices[device_id].model = model
        return threshold

    # ------------------------------------------------------------------
    # Ingest + detect
    # ------------------------------------------------------------------
    def observe_batch(self, readings: list[dict]) -> list[Anomaly]:
        """Feed one anchored batch through all three detection levels."""
        found: list[Anomaly] = []
        for reading in readings:
            state = self._devices[reading["device_id"]]
            state.history.append((reading["timestamp"], float(reading["value"])))
            found.extend(self._check_temporal(reading["device_id"], state))
        if readings:
            now = max(r["timestamp"] for r in readings)
            found.extend(self._check_silence(now))
            found.extend(self._check_spatial(now))
            found.extend(self._check_cross_modal(now))
        fresh = []
        for anomaly in found:
            dedup_key = (anomaly.kind, anomaly.device_id)
            last = self._last_reported.get(dedup_key)
            if last is not None and anomaly.timestamp - last < self.dedup_window_s:
                continue
            self._last_reported[dedup_key] = anomaly.timestamp
            self.anomalies.append(anomaly)
            self.ledger.emit_event("AnomalyDetected", anomaly.to_dict())
            fresh.append(anomaly)
        return fresh

    # -- level 1: temporal --------------------------------------------
    def _check_temporal(self, device_id: str, state: _DeviceState) -> list[Anomaly]:
        if state.model is None or len(state.history) < self.window:
            return []
        window_vals = np.array([v for _, v in state.history[-self.window:]])
        anomalous, err = state.model.is_anomalous(window_vals)
        if not anomalous:
            return []
        ts = state.history[-1][0]
        ratio = err / state.model.threshold if state.model.threshold else float("inf")
        return [Anomaly(
            kind="temporal", device_id=device_id, timestamp=ts,
            detail=f"autoencoder reconstruction error {err:.3f} exceeds threshold "
                   f"{state.model.threshold:.3f} ({ratio:.1f}x) — possible tampering or drift",
            score=err, severity="critical" if ratio > 3 else "warning",
        )]

    # -- level 1b: silence ---------------------------------------------
    def _check_silence(self, now: float) -> list[Anomaly]:
        found = []
        for device_id, state in self._devices.items():
            if not state.history:
                continue
            gap = now - state.history[-1][0]
            if gap > self.silence_threshold_s:
                found.append(Anomaly(
                    kind="silence", device_id=device_id, timestamp=now,
                    detail=f"no readings for {gap / 60:.1f} min "
                           f"(threshold {self.silence_threshold_s / 60:.0f} min)",
                    score=gap, severity="critical",
                ))
        return found

    # -- level 2: spatial -----------------------------------------------
    def _check_spatial(self, now: float) -> list[Anomaly]:
        found = []
        for site, by_type in self._sites.items():
            for sensor_type, device_ids in by_type.items():
                latest = {}
                for device_id in device_ids:
                    hist = self._devices[device_id].history
                    if hist and now - hist[-1][0] <= self.silence_threshold_s:
                        latest[device_id] = hist[-1][1]
                if len(latest) < 3:
                    continue
                values = np.array(list(latest.values()))
                median = float(np.median(values))
                mad = float(np.median(np.abs(values - median)))
                # MAD → sigma-equivalent, with a 2% relative floor so a
                # tight consensus (near-zero MAD) doesn't inflate z scores
                scale = max(mad * 1.4826, 0.02 * max(abs(median), 1.0))
                for device_id, value in latest.items():
                    z = abs(value - median) / scale
                    if z > self.spatial_z_threshold:
                        found.append(Anomaly(
                            kind="spatial", device_id=device_id, timestamp=now,
                            detail=f"{sensor_type} reading {value:.1f} diverges from "
                                   f"co-located consensus {median:.1f} at site {site} "
                                   f"(robust z={z:.1f})",
                            score=z, severity="critical" if z > 2 * self.spatial_z_threshold else "warning",
                        ))
        return found

    # -- level 3: cross-modal --------------------------------------------
    def _check_cross_modal(self, now: float) -> list[Anomaly]:
        """Rule bank for physically-linked modalities at the same site."""
        found = []
        for site, by_type in self._sites.items():
            aqi = self._site_mean(by_type.get("aqi", []), now)
            traffic = self._site_mean(by_type.get("traffic", []), now)
            if aqi is None or traffic is None:
                continue
            # vehicular pollution with an empty road is physically implausible
            if aqi > 200 and traffic < 5:
                device_ids = by_type.get("aqi", [])
                found.append(Anomaly(
                    kind="cross_modal", device_id=",".join(device_ids), timestamp=now,
                    detail=f"site {site}: AQI {aqi:.0f} (severe, vehicular profile) while "
                           f"traffic count is {traffic:.0f} veh/min — modality inconsistency",
                    score=aqi / max(traffic, 1.0), severity="warning",
                ))
        return found

    def _site_mean(self, device_ids: list[str], now: float) -> float | None:
        values = [self._devices[d].history[-1][1] for d in device_ids
                  if self._devices[d].history and now - self._devices[d].history[-1][0] <= self.silence_threshold_s]
        return float(np.mean(values)) if values else None
