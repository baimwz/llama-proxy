import time

from llama_proxy.ratelimit import RateLimiter


def test_unlimited():
    rl = RateLimiter()
    for _ in range(100):
        ok, _ = rl.allow("a", 0)
        assert ok


def test_limit_and_retry_after():
    rl = RateLimiter()
    assert rl.allow("a", 2) == (True, 0)
    assert rl.allow("a", 2)[0] is True
    ok, retry = rl.allow("a", 2)
    assert ok is False
    assert 1 <= retry <= 3601


def test_users_independent():
    rl = RateLimiter()
    rl.allow("a", 1)
    assert rl.allow("a", 1)[0] is False
    assert rl.allow("b", 1)[0] is True


def test_window_slides():
    rl = RateLimiter()
    assert rl.allow("a", 1)[0] is True
    rl._stamps["a"][0] = time.time() - 3601  # fake an hour-old request
    assert rl.allow("a", 1)[0] is True
