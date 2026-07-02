"""Vendor SLA Enforcement contract.

The system integrator's SLA is encoded on-chain: minimum data
completeness, maximum sensor-silence duration, and maximum share of
uncalibrated devices. Metric reports (computed from anchored data, so
they are themselves verifiable) are recorded per vendor per period;
any threshold violation automatically emits an SLABreach event with
the computed penalty — no manual dispute process.
"""

from __future__ import annotations

import time

from ..ledger import Ledger

DEFAULT_TERMS = {
    "min_completeness_pct": 95.0,     # % of expected readings actually anchored
    "max_silence_minutes": 15.0,      # longest tolerated gap per sensor
    "max_uncalibrated_pct": 0.0,      # % of fleet allowed to run uncalibrated
    "penalty_per_breach_inr": 50_000,
}


class SLAContract:
    CONTRACT = "sla"

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def register_sla(self, vendor: str, terms: dict | None = None) -> dict:
        sla = {"vendor": vendor, "terms": {**DEFAULT_TERMS, **(terms or {})},
               "registered_at": time.time(), "active": True}
        self.ledger.put_state(self.CONTRACT, "registerSLA", f"sla~{vendor}", sla)
        self.ledger.emit_event("SLARegistered", {"vendor": vendor, "terms": sla["terms"]})
        return sla

    def get_sla(self, vendor: str) -> dict | None:
        return self.ledger.get_state(f"sla~{vendor}")

    def record_metrics(self, vendor: str, period: str, metrics: dict) -> dict:
        """Record a period's metrics and auto-evaluate the SLA.

        metrics: completeness_pct, max_silence_minutes, uncalibrated_pct
        (computed off anchored batches, so auditable by any stakeholder).
        """
        sla = self.get_sla(vendor)
        if sla is None:
            raise KeyError(f"no SLA registered for vendor {vendor}")
        terms = sla["terms"]

        violations = []
        if metrics.get("completeness_pct", 100.0) < terms["min_completeness_pct"]:
            violations.append({
                "term": "min_completeness_pct",
                "required": terms["min_completeness_pct"],
                "observed": metrics["completeness_pct"],
            })
        if metrics.get("max_silence_minutes", 0.0) > terms["max_silence_minutes"]:
            violations.append({
                "term": "max_silence_minutes",
                "required": terms["max_silence_minutes"],
                "observed": metrics["max_silence_minutes"],
            })
        if metrics.get("uncalibrated_pct", 0.0) > terms["max_uncalibrated_pct"]:
            violations.append({
                "term": "max_uncalibrated_pct",
                "required": terms["max_uncalibrated_pct"],
                "observed": metrics["uncalibrated_pct"],
            })

        report = {
            "vendor": vendor,
            "period": period,
            "metrics": metrics,
            "violations": violations,
            "breached": bool(violations),
            "penalty_inr": terms["penalty_per_breach_inr"] * len(violations),
            "recorded_at": time.time(),
        }
        self.ledger.put_state(self.CONTRACT, "recordMetrics", f"slareport~{vendor}~{period}", report)
        if violations:
            self.ledger.emit_event("SLABreach", {
                "vendor": vendor, "period": period,
                "violations": violations, "penalty_inr": report["penalty_inr"],
            })
        return report

    def breaches(self, vendor: str | None = None) -> list[dict]:
        reports = [v for _, v in self.ledger.query_by_prefix("slareport~")]
        out = [r for r in reports if r["breached"]]
        if vendor is not None:
            out = [r for r in out if r["vendor"] == vendor]
        return sorted(out, key=lambda r: (r["vendor"], r["period"]))
