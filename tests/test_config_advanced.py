"""Advanced tests for config.py — untested paths and edge cases.

Cycle 9: Covers data_path/memory_path properties, _interpolate_dict edge cases,
YAML loading with env vars, empty YAML, SelfImproveConfig, and full config roundtrip.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from secretary.config import (
    SecretaryConfig,
    ModelTier,
    RoutingConfig,
    WatcherConfig,
    MemoryConfig,
    SelfImproveConfig,
    CurrencyConfig,
    _interpolate_env,
    _interpolate_dict,
)


# ── _interpolate_env edge cases ────────────────────────────────


def test_interpolate_env_multiple_vars():
    """Multiple ${VAR} in one string should all be replaced."""
    os.environ["TEST_A"] = "hello"
    os.environ["TEST_B"] = "world"
    try:
        result = _interpolate_env("${TEST_A} ${TEST_B}")
        assert result == "hello world"
    finally:
        del os.environ["TEST_A"]
        del os.environ["TEST_B"]


def test_interpolate_env_default_with_spaces():
    """${VAR:- spaced default } should strip default value."""
    result = _interpolate_env("${NONEXISTENT_XYZZY:- spaced }")
    assert result == "spaced"


def test_interpolate_env_existing_var_ignores_default():
    """When the env var exists, default should be ignored."""
    os.environ["TEST_EXIST"] = "real_value"
    try:
        result = _interpolate_env("${TEST_EXIST:-fallback}")
        assert result == "real_value"
    finally:
        del os.environ["TEST_EXIST"]


def test_interpolate_env_no_vars():
    """String with no ${} patterns should pass through unchanged."""
    assert _interpolate_env("plain text") == "plain text"


def test_interpolate_env_empty_string():
    assert _interpolate_env("") == ""


def test_interpolate_env_nested_braces_not_supported():
    """Nested ${} is not supported; inner brace handled as literal."""
    result = _interpolate_env("${OUTER_${INNER}}")
    # The regex won't match nested braces properly
    assert isinstance(result, str)


# ── _interpolate_dict edge cases ───────────────────────────────


def test_interpolate_dict_nested():
    """Nested dicts should be interpolated recursively."""
    os.environ["TEST_NESTED"] = "found"
    try:
        result = _interpolate_dict({
            "level1": {
                "level2": {
                    "val": "${TEST_NESTED}"
                }
            }
        })
        assert result["level1"]["level2"]["val"] == "found"
    finally:
        del os.environ["TEST_NESTED"]


def test_interpolate_dict_list_with_non_strings():
    """Lists containing non-string items should pass through."""
    result = _interpolate_dict({
        "nums": [42, 3.14, True, None],
        "mixed": ["hello", 42, "${NONEXISTENT_VAR:-default}"],
    })
    assert result["nums"] == [42, 3.14, True, None]
    assert result["mixed"][0] == "hello"
    assert result["mixed"][1] == 42
    assert result["mixed"][2] == "default"


def test_interpolate_dict_non_string_values():
    """Int, float, bool, None values should pass through unchanged."""
    result = _interpolate_dict({
        "count": 10,
        "rate": 3.14,
        "flag": True,
        "nothing": None,
    })
    assert result == {"count": 10, "rate": 3.14, "flag": True, "nothing": None}


def test_interpolate_dict_empty():
    assert _interpolate_dict({}) == {}


# ── data_path property ─────────────────────────────────────────


def test_data_path_default():
    """Without instance_id, data_path is just data_root."""
    config = SecretaryConfig(data_root="mydata")
    assert config.data_path == Path("mydata")


def test_data_path_with_instance_id():
    """With instance_id, data_path is data_root/instance_id."""
    config = SecretaryConfig(data_root="data", instance_id="worker-1")
    assert config.data_path == Path("data/worker-1")


def test_data_path_empty_instance_id():
    """Empty instance_id should behave like no instance_id."""
    config = SecretaryConfig(data_root="data", instance_id="")
    assert config.data_path == Path("data")


# ── memory_path property ───────────────────────────────────────


def test_memory_path_default():
    """Default memory path without instance_id."""
    config = SecretaryConfig()
    assert config.memory_path == Path("data/memory.json")


def test_memory_path_with_instance_id():
    """With instance_id, memory path gets namespaced."""
    config = SecretaryConfig(instance_id="bot-2")
    assert config.memory_path == Path("data/memory-bot-2.json")


def test_memory_path_custom_memory_config():
    """Custom memory.path should be respected with instance_id."""
    config = SecretaryConfig(
        memory=MemoryConfig(path="custom/mem.json"),
        instance_id="inst-A",
    )
    assert config.memory_path == Path("custom/mem-inst-A.json")


def test_memory_path_no_instance_id_custom():
    """Custom memory.path without instance_id should be used as-is."""
    config = SecretaryConfig(
        memory=MemoryConfig(path="custom/mem.json"),
    )
    assert config.memory_path == Path("custom/mem.json")


# ── YAML loading edge cases ───────────────────────────────────


def test_load_empty_yaml_file(tmp_path: Path):
    """Empty YAML file (safe_load returns None) should use defaults."""
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("", encoding="utf-8")
    config = SecretaryConfig.load(config_file)
    assert config.routing.default_tier == "medium"
    assert config.watcher.interval_minutes == 30


def test_load_yaml_with_env_interpolation(tmp_path: Path):
    """Env vars in YAML values should be interpolated."""
    os.environ["TEST_API_URL"] = "http://test:9999"
    try:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "anthropic_base_url: ${TEST_API_URL}\n",
            encoding="utf-8",
        )
        config = SecretaryConfig.load(config_file)
        assert config.anthropic_base_url == "http://test:9999"
    finally:
        del os.environ["TEST_API_URL"]


def test_load_yaml_with_all_sections(tmp_path: Path):
    """Full config with all sections should load correctly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
data_root: custom_data
agent_prefix: false
file_tools: true
instance_id: test-1
routing:
  default_tier: low
  tiers:
    low:
      model: claude-haiku-4.5
      max_turns: 5
      max_budget_usd: 0.5
      description: testing low
    medium:
      model: claude-sonnet-4.6
      max_turns: 15
watcher:
  interval_minutes: 10
  max_runs: 100
  pause_on_failure: false
  campaign_file: my_campaign.yaml
  max_retries: 3
  retry_base_delay: 10.0
  task_timeout: 600
  notify_email: test@example.com
memory:
  short_max: 10
  long_max: 25
  path: custom/memory.json
self_improve:
  auto_promote: true
  test_timeout: 60
  keep_sandbox: true
currency:
  display_currency: USD
  usd_to_cad_rate: 1.50
""", encoding="utf-8")
    config = SecretaryConfig.load(config_file)
    assert config.data_root == "custom_data"
    assert config.agent_prefix is False
    assert config.file_tools is True
    assert config.instance_id == "test-1"
    assert config.routing.default_tier == "low"
    assert config.routing.tiers["low"].max_turns == 5
    assert config.routing.tiers["low"].max_budget_usd == 0.5
    assert config.watcher.interval_minutes == 10
    assert config.watcher.max_runs == 100
    assert config.watcher.pause_on_failure is False
    assert config.watcher.campaign_file == "my_campaign.yaml"
    assert config.watcher.max_retries == 3
    assert config.watcher.retry_base_delay == 10.0
    assert config.watcher.task_timeout == 600
    assert config.watcher.notify_email == "test@example.com"
    assert config.memory.short_max == 10
    assert config.memory.long_max == 25
    assert config.memory.path == "custom/memory.json"
    assert config.self_improve.auto_promote is True
    assert config.self_improve.test_timeout == 60
    assert config.self_improve.keep_sandbox is True
    assert config.currency.display_currency == "USD"
    assert config.currency.usd_to_cad_rate == 1.50


def test_load_yaml_partial_sections(tmp_path: Path):
    """Partial YAML (only some sections) should merge with defaults."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
watcher:
  interval_minutes: 45
""", encoding="utf-8")
    config = SecretaryConfig.load(config_file)
    # Watcher overridden
    assert config.watcher.interval_minutes == 45
    # Everything else defaults
    assert config.routing.default_tier == "medium"
    assert config.memory.short_max == 20


# ── SelfImproveConfig defaults ─────────────────────────────────


def test_self_improve_defaults():
    config = SelfImproveConfig()
    assert config.auto_promote is False
    assert config.test_timeout == 300
    assert config.sandbox_dir == ""
    assert config.keep_sandbox is False


def test_self_improve_custom():
    config = SelfImproveConfig(
        auto_promote=True,
        test_timeout=300,
        sandbox_dir="/tmp/sandbox",
        keep_sandbox=True,
    )
    assert config.auto_promote is True
    assert config.test_timeout == 300
    assert config.sandbox_dir == "/tmp/sandbox"
    assert config.keep_sandbox is True


# ── MemoryConfig defaults ─────────────────────────────────────


def test_memory_config_defaults():
    config = MemoryConfig()
    assert config.short_max == 20
    assert config.long_max == 50
    assert config.path == "data/memory.json"


# ── WatcherConfig additional validations ───────────────────────


def test_watcher_max_premium_per_cycle_default():
    config = WatcherConfig()
    assert config.max_premium_per_cycle == 0.0


def test_watcher_notify_email_default():
    config = WatcherConfig()
    assert config.notify_email == ""


# ── MCP servers config ─────────────────────────────────────────


def test_mcp_servers_default_empty():
    config = SecretaryConfig()
    assert config.mcp_servers == {}


def test_mcp_servers_from_yaml(tmp_path: Path):
    """MCP server config should load from YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
mcp_servers:
  gmail:
    command: npx
    args:
      - gmail-mcp
    env:
      GMAIL_TOKEN: abc123
""", encoding="utf-8")
    config = SecretaryConfig.load(config_file)
    assert "gmail" in config.mcp_servers
    assert config.mcp_servers["gmail"]["command"] == "npx"
    assert config.mcp_servers["gmail"]["args"] == ["gmail-mcp"]


# ── ModelTier edge cases ───────────────────────────────────────


def test_model_tier_description_default():
    tier = ModelTier(model="test-model")
    assert tier.description == ""
    assert tier.max_turns == 30
    assert tier.max_budget_usd == 0.0


def test_model_tier_large_budget():
    tier = ModelTier(model="test-model", max_budget_usd=100.0)
    assert tier.max_budget_usd == 100.0


# ── SecretaryConfig.interpolate_strings (model_validator) ──────


def test_interpolate_strings_non_dict():
    """If raw data is not a dict, it should pass through unchanged."""
    # This tests the @classmethod model_validator branch
    # Normally pydantic passes a dict, but the validator handles non-dict
    result = SecretaryConfig.interpolate_strings("not a dict")
    assert result == "not a dict"


def test_interpolate_strings_with_env_in_nested(tmp_path: Path):
    """Env vars should be interpolated in deeply nested config."""
    os.environ["TEST_DEEP_VAL"] = "deep_value"
    try:
        config_file = tmp_path / "config.yaml"
        # Must include medium tier (the default_tier) to pass validation
        config_file.write_text("""
routing:
  tiers:
    low:
      model: claude-haiku-4.5
      description: ${TEST_DEEP_VAL}
    medium:
      model: claude-sonnet-4.6
    high:
      model: claude-opus-4.7
""", encoding="utf-8")
        config = SecretaryConfig.load(config_file)
        assert config.routing.tiers["low"].description == "deep_value"
    finally:
        del os.environ["TEST_DEEP_VAL"]


# ── SecretaryConfig defaults ──────────────────────────────────


def test_file_workspace_default():
    config = SecretaryConfig()
    assert config.file_workspace == ""


def test_file_tools_default():
    config = SecretaryConfig()
    assert config.file_tools is False


def test_agent_prefix_default():
    config = SecretaryConfig()
    assert config.agent_prefix is True
