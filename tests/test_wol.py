import pytest

from llama_proxy.power import build_magic_packet


def test_magic_packet_format():
    pkt = build_magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(pkt) == 6 + 6 * 16
    assert pkt[:6] == b"\xff" * 6
    mac = bytes.fromhex("aabbccddeeff")
    assert pkt[6:12] == mac
    # 16 repetitions of the MAC
    for i in range(16):
        assert pkt[6 + i * 6 : 6 + (i + 1) * 6] == mac


def test_magic_packet_dash_separated():
    pkt = build_magic_packet("AA-BB-CC-DD-EE-FF")
    assert pkt[6:12] == bytes.fromhex("AABBCCDDEEFF")


def test_magic_packet_invalid():
    with pytest.raises(ValueError):
        build_magic_packet("zz")
    with pytest.raises(ValueError):
        build_magic_packet("aabbcc")
