import pytest

from llama_proxy.keys import KeyStore, clamp_priority


def test_create_and_lookup(tmp_path):
    path = tmp_path / "keys.json"
    store = KeyStore(path)
    record, key = store.create("alice", priority=5)
    assert key.startswith("lp-")
    assert record.prefix in key
    assert record.priority == 5
    assert store.lookup(key).name == "alice"
    # the plaintext key must never be stored
    assert key not in path.read_text()


def test_persist_and_reload(tmp_path):
    path = tmp_path / "keys.json"
    _, key = KeyStore(path).create("bob")
    assert KeyStore(path).lookup(key).name == "bob"


def test_disable_enable_remove(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    _, key = store.create("carl")
    store.set_disabled("carl", True)
    assert store.lookup(key).disabled
    store.set_disabled("carl", False)
    assert not store.lookup(key).disabled
    store.remove("carl")
    assert store.lookup(key) is None
    assert len(store) == 0


def test_update_limits(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.create("dana")
    record = store.update("dana", requests_per_hour=60, tokens_per_day=100_000)
    assert record.requests_per_hour == 60
    assert record.tokens_per_day == 100_000
    with pytest.raises(ValueError):
        store.update("dana", bogus=1)
    with pytest.raises(KeyError):
        store.update("nobody")


def test_name_validation_and_duplicates(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    for bad in ("has space", "x" * 40, ""):
        with pytest.raises(ValueError):
            store.create(bad)
    # names are normalized to lowercase
    record, _ = store.create("ALICE")
    assert record.name == "alice"
    with pytest.raises(ValueError):
        store.create("Alice")  # duplicate after normalization
    # lookup/update/remove are case-insensitive too
    assert store.get("Alice") is not None


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text("{not json")
    store = KeyStore(path)
    assert len(store) == 0
    _, key = store.create("eve")  # still usable, fresh salt
    assert store.lookup(key).name == "eve"


def test_reload_on_external_change(tmp_path):
    path = tmp_path / "keys.json"
    store = KeyStore(path)
    _, key = store.create("frank")
    # another process (CLI) removes the key from the file
    KeyStore(path).remove("frank")
    assert store.lookup(key) is None  # picked up automatically


def test_clamp_priority():
    assert clamp_priority(42) == 10
    assert clamp_priority(-5) == 0
    assert clamp_priority("7") == 7
    assert clamp_priority(None) == 0
