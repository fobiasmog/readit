"""Configuration and paths shared by the /listen scripts.

Values come from, in increasing priority: built-in defaults, the TOML file
(~/.config/voice-cc/listen.toml or $VOICE_CC_CONFIG), and VOICE_CC_* environment
variables. Standard library only, so the extractor can run without mlx-audio.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

QUANTS = ("bf16", "8bit", "6bit", "4bit")
VOICES = ("tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe")
MODEL_REPO = "mlx-community/orpheus-3b-0.1-ft-{quant}"
SNAC_REPO = "mlx-community/snac_24khz"
# Orpheus loads its tokenizer from the bf16 repo whatever the build (mlx-audio
# ModelConfig.tokenizer_name), so the tokenizer files must be cached too.
TOKENIZER_REPO = "mlx-community/orpheus-3b-0.1-ft-bf16"
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json")

# Defaults. `quant` and `words` come from bench/RESULTS.md (task 1.6).
DEFAULTS = {
    "quant": "4bit",
    "voice": "tara",
    "words": 300,
    "digest_chars": 60000,
    "keep": 20,
}

ENV_PREFIX = "VOICE_CC_"


class ConfigError(Exception):
    """A config value is invalid. The message is one line, fit for the user."""


@dataclass(frozen=True)
class Config:
    quant: str
    voice: str
    words: int
    digest_chars: int
    keep: int

    @property
    def model_repo(self) -> str:
        return MODEL_REPO.format(quant=self.quant)


def config_path() -> Path:
    override = os.environ.get(ENV_PREFIX + "CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "voice-cc" / "listen.toml"


def output_dir() -> Path:
    override = os.environ.get(ENV_PREFIX + "OUT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "voice-cc" / "listen"


def state_dir() -> Path:
    override = os.environ.get(ENV_PREFIX + "STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "voice-cc" / "listen"


def _int(name: str, value: object, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"listen config: {name} must be a whole number, got {value!r}") from None
    if isinstance(value, bool) or not low <= number <= high:
        raise ConfigError(f"listen config: {name} must be between {low} and {high}, got {value!r}")
    return number


def _validate(raw: dict) -> Config:
    quant = str(raw["quant"]).strip().lower()
    if quant not in QUANTS:
        raise ConfigError(f"listen config: quant must be one of {', '.join(QUANTS)}, got {raw['quant']!r}")
    voice = str(raw["voice"]).strip().lower()
    if voice not in VOICES:
        raise ConfigError(f"listen config: voice must be one of {', '.join(VOICES)}, got {raw['voice']!r}")
    return Config(
        quant=quant,
        voice=voice,
        words=_int("words", raw["words"], 30, 3000),
        digest_chars=_int("digest_chars", raw["digest_chars"], 2000, 1_000_000),
        keep=_int("keep", raw["keep"], 1, 10_000),
    )


def load(path: Path | None = None, environ: dict | None = None) -> Config:
    """Return the effective config: defaults < file < environment."""
    environ = os.environ if environ is None else environ
    path = config_path() if path is None else path
    raw = dict(DEFAULTS)
    if path.is_file():
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"listen config: {path} is not valid TOML ({exc})") from None
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            raise ConfigError(f"listen config: unknown key(s) in {path}: {', '.join(unknown)}")
        raw.update(data)
    for field in fields(Config):
        value = environ.get(ENV_PREFIX + field.name.upper())
        if value not in (None, ""):
            raw[field.name] = value
    return _validate(raw)
