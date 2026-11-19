"""V2 settings: safe V1 defaults, and invalid values fail early with a readable ConfigError."""
import pytest

import config


def test_v2_features_are_off_by_default():
    assert config.RETRIEVAL_MODE == "v1" and config.PATCH_MODE == "v1" and config.INDEXING_ENABLED is False


def test_index_dir_is_under_the_data_dir_not_the_workspace():
    assert config.INDEX_DIR == config.DATA_DIR / "index"


def test_limits_have_sane_defaults():
    for name in ("CHUNK_MAX_TOKENS", "CHUNK_MAX_LINES", "INDEX_MAX_FILE_BYTES", "INDEX_MAX_FILES", "TOP_K", "RRF_K",
                 "RETRIEVAL_CONTEXT_BUDGET"):
        assert getattr(config, name) > 0, name
    assert config.EMBEDDING_MODEL


def test_choice_accepts_allowed_values_and_rejects_others():
    assert config.env_choice("X_MODE", "v1", ("v1", "v2"), {"X_MODE": "V2"}) == "v2"
    assert config.env_choice("X_MODE", "v1", ("v1", "v2"), {}) == "v1"
    with pytest.raises(config.ConfigError, match="X_MODE.*v1, v2"):
        config.env_choice("X_MODE", "v1", ("v1", "v2"), {"X_MODE": "v3"})


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5", ""])
def test_positive_int_rejects_garbage(bad):
    with pytest.raises(config.ConfigError, match="X_N"):
        config.env_int("X_N", 10, {"X_N": bad})


def test_positive_int_default_and_override():
    assert config.env_int("X_N", 10, {}) == 10
    assert config.env_int("X_N", 10, {"X_N": "25"}) == 25


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False), ("off", False)])
def test_bool_parsing(raw, expected):
    assert config.env_bool("X_B", False, {"X_B": raw}) is expected


def test_bool_rejects_garbage():
    with pytest.raises(config.ConfigError, match="X_B"):
        config.env_bool("X_B", False, {"X_B": "maybe"})
