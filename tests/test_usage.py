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


def test_cached_tokens_tracked(tmp_path):
    u = UsageTracker(str(tmp_path), 60)
    # 73 prompt tokens, 69 of them served from KV cache
    u.record("default", 73, 5, 1, cached_tokens=69)
    u.flush()
    reloaded = UsageTracker(str(tmp_path), 60)
    totals = reloaded.totals()
    assert totals["default"]["prompt"] == 73
    assert totals["default"]["completion"] == 5
    assert totals["default"]["cached"] == 69


def test_cached_tokens_default_zero(tmp_path):
    # callers that don't pass cached_tokens (old code path) must still work
    u = UsageTracker(str(tmp_path), 60)
    u.record("default", 100, 50, 2)
    assert u.totals()["default"]["cached"] == 0


def test_legacy_rows_without_cached_field(tmp_path):
    # rows written before the `cached` field existed must not break totals()
    from datetime import date

    u = UsageTracker(str(tmp_path), 60)
    u._data["u1"] = {date.today().isoformat(): {"prompt": 5, "completion": 1, "requests": 1}}
    totals = u.totals(days=30)
    assert totals["u1"]["prompt"] == 5
    assert totals["u1"]["cached"] == 0


def test_record_on_legacy_row_backfills_cached(tmp_path):
    # Regression: a row written by an older version has no "cached" key.
    # record() used to raise KeyError('cached') there, silently dropping the
    # request after the client already received its response. It must now
    # backfill the field and record every counter.
    u = UsageTracker(str(tmp_path), 60)
    day = date.today().isoformat()
    u._data["u1"] = {day: {"prompt": 5, "completion": 1, "requests": 1}}
    u.record("u1", 10, 4, 1, cached_tokens=6)  # must not raise
    row = u._data["u1"][day]
    assert row["prompt"] == 15
    assert row["completion"] == 5
    assert row["cached"] == 6
    assert row["requests"] == 2
    totals = u.totals(days=30)
    assert totals["u1"]["cached"] == 6
    assert totals["u1"]["requests"] == 2


def test_reload_backfills_legacy_rows(tmp_path):
    # A usage.json written by an older version (no "cached" key) must load
    # cleanly and record() on its rows must work.
    day = date.today().isoformat()
    (tmp_path / "usage.json").write_text(
        '{"master": {"%s": {"prompt": 4471, "completion": 698, "requests": 9}}}' % day
    )
    u = UsageTracker(str(tmp_path), 60)
    u.record("master", 71, 4, 1, cached_tokens=42)  # must not raise
    row = u._data["master"][day]
    assert row["cached"] == 42
    assert row["requests"] == 10  # 9 (legacy) + 1
    assert row["prompt"] == 4471 + 71
