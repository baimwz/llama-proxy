"""Configuration loading and validation (TOML over dataclass defaults)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


@dataclass
class ProxyConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    master_api_key: str = ""
    idle_timeout_s: float = 1200.0
    client_wait_timeout_s: float = 600.0
    sse_heartbeat_s: float = 15.0


@dataclass
class LlmConfig:
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    model_name: str = "qwen3-27b"
    boot_timeout_s: float = 300.0
    health_interval_s: float = 2.0
    read_timeout_s: float = 300.0


@dataclass
class PowerConfig:
    ssh_host: str = ""
    ssh_user: str = "proxy"
    ssh_key_path: str = ""
    wol_mac: str = ""
    wol_bcast: str = ""
    shutdown_verify_timeout_s: float = 30.0


@dataclass
class SchedulerConfig:
    large_threshold_tokens: int = 4096
    small_max_concurrent: int = 2
    large_exclusive: bool = True
    classification: str = "delta"  # "delta" | "total"
    conv_state_ttl_s: float = 1800.0
    conv_state_max: int = 128


@dataclass
class UsageConfig:
    data_dir: str = "./data"
    flush_interval_s: float = 60.0


@dataclass
class UsersConfig:
    """Phase 2 defaults for newly created user keys (0 = unlimited)."""

    default_priority: int = 3
    default_max_priority: int = 10
    default_requests_per_hour: int = 0
    default_tokens_per_day: int = 0


@dataclass
class TokenizerConfig:
    url: str = "https://huggingface.co/Qwen/Qwen3-27B/resolve/main/tokenizer.json"


@dataclass
class Config:
    proxy: ProxyConfig
    llm: LlmConfig
    power: PowerConfig
    scheduler: SchedulerConfig
    usage: UsageConfig
    users: UsersConfig
    tokenizer: TokenizerConfig

    @classmethod
    def defaults(cls) -> Config:
        return cls(
            ProxyConfig(),
            LlmConfig(),
            PowerConfig(),
            SchedulerConfig(),
            UsageConfig(),
            UsersConfig(),
            TokenizerConfig(),
        )

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Config:
        resolved: str | os.PathLike | None = path
        if resolved is None:
            for candidate in (os.environ.get("LLAMA_PROXY_CONFIG"), "./config.toml"):
                if candidate and Path(candidate).is_file():
                    resolved = candidate
                    break
        if resolved is None:
            return cls.defaults()
        p = Path(str(resolved))
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
        cfg = cls.defaults()
        sections = {
            "proxy": cfg.proxy,
            "llm": cfg.llm,
            "power": cfg.power,
            "scheduler": cfg.scheduler,
            "usage": cfg.usage,
            "users": cfg.users,
            "tokenizer": cfg.tokenizer,
        }
        for section, value in raw.items():
            target = sections.get(section)
            if target is None:
                raise ConfigError(f"unknown config section: [{section}]")
            if not isinstance(value, dict):
                raise ConfigError(f"section [{section}] must be a table")
            for key, val in value.items():
                if not hasattr(target, key):
                    raise ConfigError(f"unknown key [{section}] {key}")
                setattr(target, key, val)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if self.scheduler.classification not in ("delta", "total"):
            raise ConfigError("scheduler.classification must be 'delta' or 'total'")
        if self.scheduler.small_max_concurrent < 1:
            raise ConfigError("scheduler.small_max_concurrent must be >= 1")
        if self.scheduler.large_threshold_tokens < 1:
            raise ConfigError("scheduler.large_threshold_tokens must be >= 1")
        if self.scheduler.conv_state_max < 1:
            raise ConfigError("scheduler.conv_state_max must be >= 1")
        if not 1 <= self.proxy.listen_port <= 65535:
            raise ConfigError("proxy.listen_port out of range")
        if not 0 <= self.users.default_priority <= 10:
            raise ConfigError("users.default_priority must be 0-10")
        if not 0 <= self.users.default_max_priority <= 10:
            raise ConfigError("users.default_max_priority must be 0-10")
        if self.users.default_requests_per_hour < 0:
            raise ConfigError("users.default_requests_per_hour must be >= 0")
        if self.users.default_tokens_per_day < 0:
            raise ConfigError("users.default_tokens_per_day must be >= 0")
        if self.llm.boot_timeout_s <= 0:
            raise ConfigError("llm.boot_timeout_s must be > 0")
        if self.power.wol_mac:
            cleaned = self.power.wol_mac.replace(":", "").replace("-", "")
            if len(cleaned) != 12:
                raise ConfigError("power.wol_mac must be a 6-byte MAC address")
            try:
                bytes.fromhex(cleaned)
            except ValueError:
                raise ConfigError("power.wol_mac is not valid hex") from None
        if self.power.ssh_host and not self.power.ssh_key_path:
            raise ConfigError("power.ssh_key_path is required when power.ssh_host is set")
        if self.power.ssh_key_path and not self.power.ssh_host:
            raise ConfigError("power.ssh_host is required when power.ssh_key_path is set")
