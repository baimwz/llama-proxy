from datetime import date, timedelta

from llama_proxy.usage import UsageTracker


def test_record_flush_reload(tmp_path):
    u = UsageTracker(str(tmp_path), 60)
    u.record("default", 100, 50, 2)
    u.flush()
    assert (tmp_path / "usage.json").is_file()
    reloaded = UsageTracker(str(tmp_path), 60)
    totals = reloaded.totals()
    assert totals["default"]["prompt"] == 100
    assert totals["default"]["completion"] == 50
    assert totals["default"]["requests"] == 2


def test_totals_window(tmp_path):
    u = UsageTracker(str(tmp_path), 60)
    old_day = (date.today() - timedelta(days=90)).isoformat()
    u._data["u1"] = {old_day: {"prompt": 5, "completion": 1, "requests": 1}}
    totals = u.totals(days=30)
    assert totals["u1"]["prompt"] == 0
    u.record("u1", 10, 5, 1)
    assert u.totals(days=30)["u1"]["prompt"] == 10


def test_corrupt_file_is_ignored(tmp_path):
    (tmp_path / "usage.json").write_text("{not json")
    u = UsageTracker(str(tmp_path), 60)  # must not raise
    u.record("default", 1, 1, 1)
    assert u.totals()["default"]["prompt"] == 1
