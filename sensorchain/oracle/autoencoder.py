"""Autoencoder for temporal anomaly detection.

A small dense autoencoder (window → bottleneck → window) trained on
windows of normal sensor behaviour. At inference, a window whose
reconstruction error exceeds the calibrated threshold is anomalous:
the network has learned the manifold of plausible sensor dynamics, and
tampered or drifting signals fall off it.

Implemented directly in NumPy so the oracle carries no ML-framework
dependency; in production this is the same architecture retrained
quarterly on CiDaP data.
"""

from __future__ import annotations

import numpy as np


class Autoencoder:
    def __init__(self, window: int = 12, hidden: int = 4, seed: int = 7):
        self.window = window
        rng = np.random.default_rng(seed)
        scale1 = np.sqrt(2.0 / window)
        scale2 = np.sqrt(2.0 / hidden)
        self.w1 = rng.normal(0.0, scale1, (window, hidden))
        self.b1 = np.zeros(hidden)
        self.w2 = rng.normal(0.0, scale2, (hidden, window))
        self.b2 = np.zeros(window)
        self.mu = 0.0
        self.sigma = 1.0
        self.threshold: float | None = None

    # ------------------------------------------------------------------
    def _forward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = np.tanh(x @ self.w1 + self.b1)
        out = h @ self.w2 + self.b2
        return h, out

    def _normalize(self, windows: np.ndarray) -> np.ndarray:
        return (windows - self.mu) / self.sigma

    def reconstruction_error(self, window_values: np.ndarray) -> float:
        x = self._normalize(np.asarray(window_values, dtype=float).reshape(1, -1))
        _, out = self._forward(x)
        return float(np.mean((out - x) ** 2))

    # ------------------------------------------------------------------
    def fit(self, series: np.ndarray, epochs: int = 300, lr: float = 0.01) -> float:
        """Train on sliding windows of a normal-behaviour series and
        calibrate the anomaly threshold from training-set errors."""
        series = np.asarray(series, dtype=float)
        if len(series) < self.window * 2:
            raise ValueError(f"need at least {self.window * 2} points to train, got {len(series)}")
        windows = np.stack([series[i:i + self.window]
                            for i in range(len(series) - self.window + 1)])
        self.mu = float(windows.mean())
        self.sigma = float(windows.std()) or 1.0
        x = self._normalize(windows)
        n = len(x)

        for _ in range(epochs):
            h, out = self._forward(x)
            err = out - x                                    # (n, window)
            # backprop of MSE through the two dense layers
            grad_w2 = h.T @ err * (2.0 / (n * self.window))
            grad_b2 = err.mean(axis=0) * 2.0 / self.window
            dh = err @ self.w2.T * (1.0 - h ** 2)
            grad_w1 = x.T @ dh * (2.0 / (n * self.window))
            grad_b1 = dh.mean(axis=0) * 2.0 / self.window
            self.w1 -= lr * grad_w1
            self.b1 -= lr * grad_b1
            self.w2 -= lr * grad_w2
            self.b2 -= lr * grad_b2

        _, out = self._forward(x)
        errors = np.mean((out - x) ** 2, axis=1)
        self.threshold = float(errors.mean() + 4.0 * errors.std())
        return self.threshold

    def is_anomalous(self, window_values: np.ndarray) -> tuple[bool, float]:
        if self.threshold is None:
            raise RuntimeError("autoencoder not trained — call fit() first")
        err = self.reconstruction_error(window_values)
        return err > self.threshold, err
