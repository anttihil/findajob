"""User configuration must survive updates to the tracked defaults."""

from pathlib import Path
from unittest import mock

from careerradar.core import config


def test_local_config_overrides_defaults_without_rewriting_them(tmp_path: Path):
    defaults = tmp_path / "config.yaml"
    local = tmp_path / "config.local.yaml"
    defaults.write_text("llm:\n  provider: auto\nscheduler:\n  enabled: false\n")

    with (
        mock.patch.object(config, "CONFIG_PATH", str(defaults)),
        mock.patch.object(config, "CONFIG_LOCAL_PATH", str(local)),
    ):
        config.save_config({"llm": {"provider": "deepseek"}})
        loaded = config.load_config()

    assert loaded["llm"]["provider"] == "deepseek"
    assert "deepseek" not in defaults.read_text()
    assert "deepseek" in local.read_text()
