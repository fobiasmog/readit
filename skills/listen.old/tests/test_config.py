"""Tests for scripts/listen_config.py (task 3.1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import listen_config  # noqa: E402


def test_missing_file_gives_defaults(tmp_path):
    config = listen_config.load(tmp_path / "missing.toml", environ={})
    assert config == listen_config.Config(**listen_config.DEFAULTS)
    assert config.voice == "tara"
    assert config.model_repo == f"mlx-community/orpheus-3b-0.1-ft-{listen_config.DEFAULTS['quant']}"


def test_file_values(tmp_path):
    path = tmp_path / "listen.toml"
    path.write_text('quant = "4bit"\nvoice = "leo"\nwords = 200\ndigest_chars = 40000\nkeep = 5\n')
    config = listen_config.load(path, environ={})
    assert (config.quant, config.voice, config.words, config.digest_chars, config.keep) == ("4bit", "leo", 200, 40000, 5)
    assert config.model_repo == "mlx-community/orpheus-3b-0.1-ft-4bit"


def test_environment_overrides_file(tmp_path):
    path = tmp_path / "listen.toml"
    path.write_text('quant = "4bit"\nwords = 200\n')
    config = listen_config.load(path, environ={"VOICE_CC_QUANT": "bf16", "VOICE_CC_WORDS": "120", "VOICE_CC_KEEP": ""})
    assert config.quant == "bf16" and config.words == 120
    assert config.keep == listen_config.DEFAULTS["keep"]  # empty env value is ignored


def test_config_path_override(tmp_path, monkeypatch):
    path = tmp_path / "other.toml"
    path.write_text('voice = "zoe"\n')
    monkeypatch.setenv("VOICE_CC_CONFIG", str(path))
    assert listen_config.load().voice == "zoe"


@pytest.mark.parametrize("content, fragment", [
    ('quant = "3bit"', "quant must be one of"),
    ('voice = "bob"', "voice must be one of"),
    ('words = "many"', "words must be a whole number"),
    ("keep = 0", "keep must be between"),
    ("colour = 1", "unknown key"),
    ("quant = ", "not valid TOML"),
])
def test_invalid_values_raise_one_line_errors(tmp_path, content, fragment):
    path = tmp_path / "listen.toml"
    path.write_text(content + "\n")
    with pytest.raises(listen_config.ConfigError) as err:
        listen_config.load(path, environ={})
    assert fragment in str(err.value) and "\n" not in str(err.value)
