import pytest

from llama_proxy.config import Config, ConfigError


def test_defaults():
    cfg = Config.defaults()
    assert cfg.scheduler.large_threshold_tokens == 4096
    assert cfg.scheduler.large_exclusive is True
    assert cfg.proxy.idle_timeout_s == 1200.0
    assert cfg.scheduler.classification == "delta"


def test_load_toml(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        "[scheduler]\nlarge_threshold_tokens = 123\n"
        "[llm]\nbase_url = \"http://x:9000\"\n"
    )
    cfg = Config.load(f)
    assert cfg.scheduler.large_threshold_tokens == 123
    assert cfg.llm.base_url == "http://x:9000"
    # untouched defaults survive
    assert cfg.proxy.idle_timeout_s == 1200.0


def test_unknown_section(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("[bogus]\nx = 1\n")
    with pytest.raises(ConfigError, match="unknown config section"):
        Config.load(f)


def test_unknown_key(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("[scheduler]\nbogus_key = 1\n")
    with pytest.raises(ConfigError, match="unknown key"):
        Config.load(f)


def test_bad_classification(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[scheduler]\nclassification = "sometimes"\n')
    with pytest.raises(ConfigError, match="classification"):
        Config.load(f)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Config.load(tmp_path / "nope.toml")


def test_power_pairing_validation(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[power]\nssh_host = "h"\n')  # missing key path
    with pytest.raises(ConfigError, match="ssh_key_path"):
        Config.load(f)


def test_wol_mac_validation(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[power]\nwol_mac = "nope"\n')
    with pytest.raises(ConfigError, match="wol_mac"):
        Config.load(f)
