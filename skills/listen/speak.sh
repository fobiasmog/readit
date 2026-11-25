#!/usr/bin/env bash
# Text on stdin -> local Orpheus TTS -> WAV -> afplay. One line of text = one spoken segment.
set -euo pipefail
out="${TMPDIR:-/tmp}/voice-cc"
mkdir -p "$out"
uv run --quiet --python 3.12 --with mlx-audio==0.6.8 \
  python -m mlx_audio.tts.generate \
  --model mlx-community/orpheus-3b-0.1-ft-4bit --voice tara \
  --output_path "$out" --file_prefix recap --audio_format wav --join_audio >/dev/null 2>&1
afplay "$out/recap.wav"
