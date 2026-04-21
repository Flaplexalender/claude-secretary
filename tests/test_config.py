"""Tests for configuration — all offline, no API calls."""
import os
from pathlib import Path
from secretary.config import SecretaryConfig, _interpolate_env


def test_default_config():
    config = SecretaryConfig()
    assert config.routing.default_tier == "medium"
    assert "low" in config.routing.tiers
    assert "medium" in config.routing.tiers
    assert "high" in config.routing.tiers


def test_tier_models():
    config = SecretaryConfig()
    assert config.routing.tiers["low"].model == "claude-haiku-4.5"
    assert config.routing.tiers["medium"].model == "claude-sonnet-4.6"
    assert config.routing.tiers["high"].model == "claude-opus-4.7"


def test_env_interpolation():
    os.environ["TEST_SEC_VAR"] = "hello"
    try:
        result = _interpolate_env("${TEST_SEC_VAR}")
        assert result == "hello"
    finally:
        del os.environ["TEST_SEC_VAR"]


def test_env_interpolation_default():
    result = _interpolate_env("${NONEXISTENT_VAR_XYZ:-fallback}")
    assert result == "fallback"


def test_env_interpolation_missing_no_default():
    result = _interpolate_env("${NONEXISTENT_VAR_XYZ}")
    assert result == "${NONEXISTENT_VAR_XYZ}"


def test_load_from_yaml(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
routing:
  default_tier: low
  tiers:
    low:
      model: claude-3-haiku-20240307
      max_turns: 5
watcher:
  interval_minutes: 15
""")
    config = SecretaryConfig.load(config_file)
    assert config.routing.default_tier == "low"
    assert config.routing.tiers["low"].max_turns == 5
    assert config.watcher.interval_minutes == 15


def test_load_nonexistent_returns_defaults():
    config = SecretaryConfig.load("nonexistent_path_xyz.yaml")
    assert config.routing.default_tier == "medium"


def test_memory_path():
    config = SecretaryConfig()
    assert config.memory_path == Path("data/memory.json")


def test_opus_budget_cap():
    config = SecretaryConfig()
    assert config.routing.tiers["high"].max_budget_usd == 5.0


def test_watcher_retry_defaults():
    config = SecretaryConfig()
    assert config.watcher.max_retries == 3
    assert config.watcher.retry_base_delay == 5.0


def test_watcher_retry_from_yaml(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
watcher:
  max_retries: 5
  retry_base_delay: 10.0
""")
    config = SecretaryConfig.load(config_file)
    assert config.watcher.max_retries == 5
    assert config.watcher.retry_base_delay == 10.0


def test_max_turns_must_be_positive():
    """ModelTier should reject max_turns < 1."""
    import pytest
    from secretary.config import ModelTier
    with pytest.raises(ValueError, match="max_turns must be >= 1"):
        ModelTier(model="claude-3-haiku-20240307", max_turns=0)


def test_max_turns_valid():
    from secretary.config import ModelTier
    tier = ModelTier(model="claude-haiku-4.5", max_turns=1)
    assert tier.max_turns == 1


# ── Budget validation ─────────────────────────────────────────


def test_negative_budget_rejected():
    """max_budget_usd < 0 should be rejected."""
    import pytest
    from secretary.config import ModelTier
    with pytest.raises(ValueError, match="max_budget_usd must be >= 0"):
        ModelTier(model="claude-haiku-4.5", max_budget_usd=-1.0)


def test_zero_budget_allowed():
    """max_budget_usd = 0 means no cap, should be valid."""
    from secretary.config import ModelTier
    tier = ModelTier(model="claude-haiku-4.5", max_budget_usd=0.0)
    assert tier.max_budget_usd == 0.0


# ── Routing default_tier validation ────────────────────────────


def test_default_tier_must_exist():
    """default_tier must reference a key in tiers dict."""
    import pytest
    from secretary.config import RoutingConfig, ModelTier
    with pytest.raises(ValueError, match="default_tier.*not in tiers"):
        RoutingConfig(
            tiers={"low": ModelTier(model="claude-haiku-4.5")},
            default_tier="nonexistent",
        )


def test_default_tier_valid():
    """default_tier matching a tier key should be fine."""
    from secretary.config import RoutingConfig, ModelTier
    cfg = RoutingConfig(
        tiers={"custom": ModelTier(model="claude-haiku-4.5")},
        default_tier="custom",
    )
    assert cfg.default_tier == "custom"


# ── Watcher validation ─────────────────────────────────────────


def test_negative_interval_rejected():
    """interval_minutes < 1 should be rejected."""
    import pytest
    from secretary.config import WatcherConfig
    with pytest.raises(ValueError, match="interval_minutes must be >= 1"):
        WatcherConfig(interval_minutes=0)


def test_negative_retries_rejected():
    """max_retries < 0 should be rejected."""
    import pytest
    from secretary.config import WatcherConfig
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        WatcherConfig(max_retries=-1)


def test_negative_task_timeout_rejected():
    """task_timeout < 0 should be rejected."""
    import pytest
    from secretary.config import WatcherConfig
    with pytest.raises(ValueError, match="task_timeout must be >= 0"):
        WatcherConfig(task_timeout=-1)


def test_zero_task_timeout_allowed():
    """task_timeout = 0 means no timeout, should be valid."""
    from secretary.config import WatcherConfig
    w = WatcherConfig(task_timeout=0)
    assert w.task_timeout == 0


# ── Currency validation ─────────────────────────────────────────


def test_zero_exchange_rate_rejected():
    """usd_to_cad_rate must be > 0."""
    import pytest
    from secretary.config import CurrencyConfig
    with pytest.raises(ValueError, match="usd_to_cad_rate must be > 0"):
        CurrencyConfig(usd_to_cad_rate=0.0)


def test_negative_exchange_rate_rejected():
    """Negative exchange rate should be rejected."""
    import pytest
    from secretary.config import CurrencyConfig
    with pytest.raises(ValueError, match="usd_to_cad_rate must be > 0"):
        CurrencyConfig(usd_to_cad_rate=-1.5)


# ── reasoning_effort validation ─────────────────────────────────


def test_reasoning_effort_default_empty():
    """Default reasoning_effort is empty string (disabled)."""
    config = SecretaryConfig()
    assert config.reasoning_effort == ""


def test_reasoning_effort_valid_values():
    """Valid reasoning_effort values should be accepted."""
    for val in ("", "low", "medium", "high", "max"):
        config = SecretaryConfig(reasoning_effort=val)
        assert config.reasoning_effort == val


def test_reasoning_effort_invalid_rejected():
    """Invalid reasoning_effort should be rejected."""
    import pytest
    with pytest.raises(ValueError, match="reasoning_effort"):
        SecretaryConfig(reasoning_effort="ultra")


def test_reasoning_effort_from_yaml(tmp_path):
    """reasoning_effort can be loaded from config YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reasoning_effort: high\n")
    config = SecretaryConfig.load(config_file)
    assert config.reasoning_effort == "high"
