import pytest

from sensorchain.merkle import MerkleProof, MerkleTree, hash_leaf


def make_readings(n):
    return [{"device_id": f"dev-{i}", "timestamp": 1000.0 + i, "value": float(i)} for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 60, 61])
def test_every_leaf_has_valid_proof(n):
    readings = make_readings(n)
    tree = MerkleTree(readings)
    for i in range(n):
        proof = tree.proof(i)
        assert proof.leaf_hash == hash_leaf(readings[i])
        assert proof.verify(tree.root)


def test_tampered_reading_fails_verification():
    readings = make_readings(10)
    tree = MerkleTree(readings)
    proof = tree.proof(4)
    tampered = dict(readings[4], value=999.0)
    forged = MerkleProof(leaf_hash=hash_leaf(tampered), path=proof.path)
    assert not forged.verify(tree.root)


def test_proof_against_wrong_root_fails():
    tree_a = MerkleTree(make_readings(7))
    tree_b = MerkleTree(make_readings(8))
    assert not tree_a.proof(0).verify(tree_b.root)


def test_proof_roundtrip_serialization():
    tree = MerkleTree(make_readings(13))
    proof = tree.proof(11)
    restored = MerkleProof.from_dict(proof.to_dict())
    assert restored.verify(tree.root)


def test_proof_is_logarithmic():
    tree = MerkleTree(make_readings(1024))
    assert len(tree.proof(500).path) == 10  # log2(1024)


def test_empty_batch_rejected():
    with pytest.raises(ValueError):
        MerkleTree([])
