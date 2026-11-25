from datetime import date, timedelta

import pytest

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


def test_phase_idle_energy_recorded(tmp_path):
    # a job entry: tokens + phase seconds + its energy share
    u = UsageTracker(str(tmp_path), 60)
    u.record(
        "anton", 100, 50, 1, cached_tokens=10,
        compute_seconds=2.0, prefill_seconds=0.5,
        generation_seconds=1.5,
        energy_wh=(0.5 * 270 + 1.5 * 320) / 3600,
    )
    # an idle-gap settlement: no tokens/requests, only idle time + energy
    u.record("anton", 0, 0, requests=0, idle_seconds=120, energy_wh=120 * 80 / 3600)
    totals = u.totals()
    assert totals["anton"]["requests"] == 1
    assert totals["anton"]["compute_seconds"] == pytest.approx(2.0)
    assert totals["anton"]["prefill_seconds"] == pytest.approx(0.5)
    assert totals["anton"]["generation_seconds"] == pytest.approx(1.5)
    assert totals["anton"]["idle_seconds"] == pytest.approx(120.0)
    expected_wh = (0.5 * 270 + 1.5 * 320) / 3600 + 120 * 80 / 3600
    assert totals["anton"]["energy_wh"] == pytest.approx(expected_wh, rel=1e-6)
    row = u.daily("anton")["anton"][0]
    assert row["prefill_seconds"] == pytest.approx(0.5)
    assert row["generation_seconds"] == pytest.approx(1.5)
    assert row["idle_seconds"] == pytest.approx(120.0)
    assert row["energy_wh"] == pytest.approx(expected_wh, rel=1e-6)


def test_today_phase_seconds(tmp_path):
    u = UsageTracker(str(tmp_path), 60)
    u.record("anton", 100, 50, 1, prefill_seconds=0.5,
             generation_seconds=1.5, idle_seconds=60)
    u.record("falko", 10, 5, 1, prefill_seconds=0.25,
             generation_seconds=0.75, idle_seconds=30)
    # per user: only their own server-on time
    assert u.today_phase_seconds("anton") == {
        "prefill_seconds": 0.5, "generation_seconds": 1.5, "idle_seconds": 60.0,
    }
    # overall (admin bar): sum over all users
    total = u.today_phase_seconds()
    assert total["prefill_seconds"] == pytest.approx(0.75)
    assert total["generation_seconds"] == pytest.approx(2.25)
    assert total["idle_seconds"] == pytest.approx(90.0)
    # user without activity today -> all zeros
    assert u.today_phase_seconds("ghost") == {
        "prefill_seconds": 0.0, "generation_seconds": 0.0, "idle_seconds": 0.0,
    }


def test_legacy_rows_without_phase_fields(tmp_path):
    # rows written before the phase/idle/energy fields existed must load
    # cleanly, aggregate as zeros, and accept new records (backfill)
    day = date.today().isoformat()
    u = UsageTracker(str(tmp_path), 60)
    u._data["u1"] = {day: {"prompt": 5, "completion": 1, "requests": 1,
                           "compute_seconds": 1.0}}
    totals = u.totals(days=30)
    assert totals["u1"]["prefill_seconds"] == 0
    assert totals["u1"]["generation_seconds"] == 0
    assert totals["u1"]["idle_seconds"] == 0
    assert totals["u1"]["energy_wh"] == 0
    # record() on such a legacy row must not raise (backfills the fields)
    u.record("u1", 0, 0, requests=0, idle_seconds=60, energy_wh=60 * 80 / 3600)
    assert u.totals(days=30)["u1"]["idle_seconds"] == pytest.approx(60.0)


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
