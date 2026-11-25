# voice-cc

`/listen [N]` in Claude Code: Claude recaps the last N turns as a short narrated story,
a local TTS model (Orpheus 3B, 4-bit, via mlx-audio) turns it into a WAV, and `afplay` plays it.

Needs macOS on Apple Silicon and [uv](https://docs.astral.sh/uv/).

    ./install.sh

- `skills/listen/SKILL.md` is the prompt.
- `skills/listen/speak.sh` reads text on stdin, writes `$TMPDIR/voice-cc/recap.wav` and plays it.
- `/listen-say [N]` (`skills/listen-say/SKILL.md`) is the same recap read by the built-in macOS `say`: instant, no model, more robotic.
- `bench/` has the quantization benchmark behind the 4-bit choice.
