import pytest

from sensorchain.contracts import AnchorContract, DeviceRegistryContract
from sensorchain.gateway import GatewayAgent
from sensorchain.identity import Device
from sensorchain.ledger import Ledger
from sensorchain.merkle import MerkleProof, hash_leaf


@pytest.fixture
def city():
    ledger = Ledger("pune")
    registry = DeviceRegistryContract(ledger)
    gateway_device = Device(
        device_id="gw-pune-01", sensor_type="gateway", manufacturer="Cisco",
        serial_number="GW-1", bis_certificate="BIS-GW-1", vendor="VendorA",
        city="pune", latitude=18.52, longitude=73.85,
    )
    registry.register_device(gateway_device.registration_record())
    agent = GatewayAgent("gw-pune-01", gateway_device.hsm, AnchorContract(ledger), window_seconds=60.0)
    return ledger, agent


def make_reading(i, t):
    return {"device_id": f"aqi-{i % 3}", "sensor_type": "aqi",
            "timestamp": t, "value": 118.0 + i, "unit": "AQI"}


def test_agent_windows_and_anchors(city):
    ledger, agent = city
    anchors = []
    # 150 seconds of readings at 10s cadence → two closed 60s windows + one open
    for i in range(15):
        anchor = agent.ingest(make_reading(i, 1000.0 + 10 * i))
        if anchor:
            anchors.append(anchor)
    final = agent.flush()
    assert final is not None
    assert len(anchors) == 2
    assert all(a["reading_count"] == 6 for a in anchors)
    assert ledger.validate_chain()[0]


def test_end_to_end_proof_verifies_and_tamper_detected(city):
    ledger, agent = city
    for i in range(6):
        agent.ingest(make_reading(i, 1000.0 + 10 * i))
    anchor = agent.flush()
    batch_id = anchor["batch_id"]

    contract = AnchorContract(ledger)
    package = agent.proof_for_reading(batch_id, 3)
    proof = MerkleProof.from_dict(package["proof"])
    assert contract.verify_reading(batch_id, proof)["verified"]

    # dashboard-layer tampering: altered value, original proof path
    tampered = dict(package["reading"], value=42.0)
    forged = MerkleProof(leaf_hash=hash_leaf(tampered), path=proof.path)
    result = contract.verify_reading(batch_id, forged)
    assert not result["verified"]
    assert "altered" in result["reason"]
