from llama_proxy.conversation import ConversationTracker, common_prefix_len
from llama_proxy.tokenizer import TokenCounter


class FakeCounter:
    """count(text) = len(text) // 2 - exact and trivial to reason about."""

    def count(self, text: str) -> int:
        return max(1, len(text) // 2)


def test_common_prefix_len_basic():
    assert common_prefix_len("abcde", "abcxf") == 3
    assert common_prefix_len("same", "same") == 4
    assert common_prefix_len("", "x") == 0
    assert common_prefix_len("abc", "ab") == 2


def test_common_prefix_len_long():
    a = "x" * 200_000
    b = "x" * 100_000 + "y"
    assert common_prefix_len(a, b) == 100_000
    assert common_prefix_len(b, a) == 100_000


def test_delta_and_note():
    tr = ConversationTracker(ttl_s=10, max_states=5)
    counter = FakeCounter()
    # prompt head must be stable across turns (append happens beyond 256 chars)
    p1 = "base context " * 30  # 270 chars
    tr.note(p1, counter.count(p1))
    p2 = p1 + " and now the new user message here"
    total2 = counter.count(p2)
    assert tr.delta_for(p2, total2, counter) == total2 - counter.count(p1)
    # different conversation -> full count
    assert tr.delta_for("zzz completely different text", 9, counter) == 9


def test_delta_requires_meaningful_prefix():
    tr = ConversationTracker(ttl_s=10, max_states=5)
    tr.note("ab", 1)  # too short to count as a match
    assert tr.delta_for("abXY", 4, FakeCounter()) == 4


def test_ttl_expiry():
    import time

    tr = ConversationTracker(ttl_s=0.01, max_states=5)
    p1 = "hello"
    tr.note(p1, 2)
    time.sleep(0.05)
    assert tr.delta_for(p1 + " more", 8, FakeCounter()) == 8  # expired -> full count


def test_lru_eviction():
    tr = ConversationTracker(ttl_s=10, max_states=2)
    for i in range(3):
        tr.note(f"conv-{i} " * 20, i)
    assert len(tr) == 2
    assert tr.key("conv-0 " * 20) not in tr._states
    assert tr.key("conv-2 " * 20) in tr._states


def test_invalidate():
    tr = ConversationTracker(ttl_s=10, max_states=5)
    tr.note("some text", 10)
    tr.invalidate()
    assert len(tr) == 0


def test_tokenizer_heuristic():
    counter = TokenCounter("", None)
    assert counter.backend == "heuristic"
    assert counter.count("abcdef") == 2
    assert counter.count("") == 1
