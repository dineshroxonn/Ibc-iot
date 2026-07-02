import pytest
from fastapi.testclient import TestClient

from sensorchain import api


@pytest.fixture(scope="module")
def client():
    return TestClient(api.app)


def test_devices_include_calibration(client):
    devices = client.get("/api/devices").json()
    assert len(devices) == 9  # 1 gateway + 8 sensors
    by_id = {d["device_id"]: d for d in devices}
    assert not by_id["aqi-004"]["calibration"]["calibrated"]
    assert by_id["aqi-001"]["calibration"]["calibrated"]
    assert "public_key_pem" not in by_id["aqi-001"]  # list view stays compact


def test_unknown_device_404(client):
    assert client.get("/api/devices/ghost").status_code == 404


def test_anchor_listing_and_proof_verification(client):
    anchors = client.get("/api/anchors").json()
    assert len(anchors) >= 30
    batch_id = anchors[0]["batch_id"]

    package = client.get(f"/api/proofs/{batch_id}/0").json()
    result = client.post("/api/verify", json={
        "batch_id": batch_id, "reading": package["reading"], "proof": package["proof"],
    }).json()
    assert result["verified"]


def test_tampered_reading_rejected(client):
    batch_id = client.get("/api/anchors").json()[0]["batch_id"]
    package = client.get(f"/api/proofs/{batch_id}/0").json()
    tampered = dict(package["reading"], value="42.000")
    result = client.post("/api/verify", json={
        "batch_id": batch_id, "reading": tampered, "proof": package["proof"],
    }).json()
    assert not result["verified"]
    assert "altered" in result["reason"]


def test_anomalies_and_sla_breaches_exposed(client):
    anomalies = client.get("/api/anomalies").json()
    assert {a["kind"] for a in anomalies} >= {"temporal", "spatial", "silence", "cross_modal"}

    breaches = client.get("/api/sla-breaches").json()
    assert {b["vendor"] for b in breaches} == {"VendorA", "VendorC"}


def test_chain_valid_and_rollup_anchored(client):
    validation = client.get("/api/chain/validate").json()
    assert validation["valid"]
    rollup = client.get("/api/rollup/pune").json()
    assert rollup and rollup[-1]["head_hash"] == validation["head_hash"]


def test_portal_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Citizen Verification Portal" in response.text
