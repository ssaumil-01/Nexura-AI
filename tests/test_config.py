import pytest
import config


def test_missing_model_env_raises_config_error(monkeypatch):
    monkeypatch.delenv("CODER_MODEL_NAME", raising=False)
    with pytest.raises(config.ConfigError, match="CODER_MODEL_NAME"):
        config.model_name("coder")
