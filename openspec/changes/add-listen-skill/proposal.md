## Why

Claude Code produces long, dense output — OpenSpec proposals, designs, explorations — that is tiring to read on screen. Hearing a short, human-style spoken explanation of what was just produced is a faster way to absorb it, but today there is no way to get one without pasting text into an external tool, and doing the summarization inside the current session would pollute its context.

## What Changes

- Add a user-level Claude Code skill, `/listen`, that turns part of the current session into a short solo spoken explainer (~2 minutes, "a person explaining the idea") and plays it.
- Support three scopes: the last turn (default), the last N turns, and the whole session.
- Run all of the work — reading the session, writing the script, synthesizing audio — in a forked background subagent so the invoking session only sees the command and a one-line notification.
- Read the session from its on-disk transcript rather than from inherited conversation context, so "whole session" keeps full fidelity after compaction and files Claude wrote (e.g. OpenSpec artifacts) are included.
- Synthesize speech locally with Canopy Labs' Orpheus 3B (fine-tuned) via MLX on Apple Silicon; no network calls at speak time.
- Play audio in a detached background process; add `/listen stop` to cancel. Keep the script and audio file for replay.
- Benchmark Orpheus bf16, 8-bit, 6-bit and 4-bit builds on the target machine and pick the default quantization from measured speed and blind listening; keep quantization configurable.

**Non-goals**
- Two-host or multi-voice podcast formats.
- Verbatim read-aloud of Claude's output.
- A persistent, always-loaded TTS server.
- Voice cloning, non-English output, or non-macOS playback.
- Auto-triggering after every response (the skill is invoked explicitly).

## Capabilities

### New Capabilities
- `listen-skill`: On-demand spoken explainer of the current Claude Code session — scope selection (last / last N / all), transcript extraction, spoken-style script writing, local Orpheus synthesis, background playback and stop, and context isolation from the invoking session.

### Modified Capabilities
<!-- None: the project has no existing specs. -->

## Impact

- **New code (this repo)**: the skill definition (`SKILL.md`), a transcript extractor script, a TTS/playback runner script, and a quant benchmark script.
- **Installation**: the skill is installed user-level at `~/.claude/skills/listen/` (copied or symlinked from this repo) so it works in every project.
- **Dependencies**: `mlx-audio` (pinned) in a dedicated `uv`-managed environment, separate from the existing `~/.venv-vllm-metal`; Orpheus MLX weights (`mlx-community/orpheus-3b-0.1-ft-*`, roughly 2–7 GB each) and the SNAC 24 kHz codec in the Hugging Face cache; macOS `afplay`.
- **Runtime resources**: several GB of unified memory while generating; generation is expected to be slower than real time on an M2 Pro, so audio starts after a delay of tens of seconds to minutes depending on quantization.
- **Claude Code features relied on**: skill frontmatter `context: fork`, `background`, `model`, and `${CLAUDE_SESSION_ID}` substitution; the `~/.claude/projects/*/<session-id>.jsonl` transcript layout. The built-in `/recap` command is avoided by naming the skill `/listen`.
