from llama_proxy.__main__ import main


def _config(tmp_path) -> str:
    f = tmp_path / "config.toml"
    f.write_text(f'[usage]\ndata_dir = "{tmp_path / "data"}"\n')
    return str(f)


def test_cli_keys_add_list_disable_enable_remove(tmp_path, capsys):
    cfg = _config(tmp_path)
    assert main(["keys", "add", "alice", "--config", cfg, "--priority", "5"]) == 0
    out = capsys.readouterr().out
    key = next(line.strip() for line in out.splitlines() if line.strip().startswith("lp-"))
    assert len(key) > 20

    assert main(["keys", "list", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and key[:12] in out

    assert main(["keys", "disable", "alice", "--config", cfg]) == 0
    assert main(["keys", "enable", "alice", "--config", cfg]) == 0
    assert main(["keys", "remove", "alice", "--config", cfg]) == 0
    assert main(["keys", "list", "--config", cfg]) == 0
    assert "no user keys yet" in capsys.readouterr().out


def test_cli_keys_errors(tmp_path, capsys):
    cfg = _config(tmp_path)
    assert main(["keys", "add", "BAD NAME", "--config", cfg]) == 1
    assert "error" in capsys.readouterr().err

    assert main(["keys", "remove", "nobody", "--config", cfg]) == 1
    assert "error" in capsys.readouterr().err


def test_cli_keys_limits(tmp_path, capsys):
    cfg = _config(tmp_path)
    assert (
        main(
            [
                "keys", "add", "svc", "--config", cfg,
                "--requests-per-hour", "60", "--tokens-per-day", "1000000",
            ]
        )
        == 0
    )
    assert main(["keys", "list", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "60" in out and "1000000" in out


def test_cli_usage(tmp_path, capsys):
    from llama_proxy.usage import UsageTracker

    cfg = _config(tmp_path)
    tracker = UsageTracker(str(tmp_path / "data"), 60)
    tracker.record("alice", 100, 50, 2)
    tracker.flush()

    assert main(["usage", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "100" in out and "50" in out

    assert main(["usage", "--user", "alice", "--days", "30", "--config", cfg]) == 0
    assert "alice" in capsys.readouterr().out


def test_cli_usage_empty(tmp_path, capsys):
    cfg = _config(tmp_path)
    assert main(["usage", "--config", cfg]) == 0
    assert "no usage data" in capsys.readouterr().out
