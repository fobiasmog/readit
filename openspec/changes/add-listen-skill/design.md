## Context

See proposal.md for motivation. Facts that shape the approach, verified on the target machine:

- **Hardware**: Apple M2 Pro, 32 GB unified memory, ~200 GB/s memory bandwidth. Token generation for a 3B model is bandwidth-bound.
- **Orpheus on MLX already works**: `mlx-audio` 0.4.6 (currently installed in `~/.venv-vllm-metal`) implements Orpheus as a Llama-3B model plus the SNAC 24 kHz codec (`mlx-community/snac_24khz`), with named voices (`tara`, `leah`, `jess`, `leo`, `dan`, `mia`, `zac`, `zoe`). The CLI exposes `--model`, `--voice`, `--max_tokens`, `--stream`, `--join_audio`, `--verbose`.
- **Model builds available**: `mlx-community/orpheus-3b-0.1-ft-{bf16,8bit,6bit,4bit}`. The upstream `canopylabs/orpheus-3b-0.1-ft` is not gated.
- **Throughput budget**: Orpheus emits 7 SNAC tokens per ~85 ms frame, i.e. ~82 tokens per second of audio. Rough estimates on this machine: bf16 ~20 tok/s (~0.25× real time), 8-bit ~37 tok/s (~0.45×), 4-bit ~65 tok/s (~0.8×). These are unmeasured; task group 1 replaces them with real numbers.
- **Transcripts**: each session is `~/.claude/projects/<project-slug>/<session-id>.jsonl`. Relevant records have `type` `user` or `assistant` with `message.content` as a string or a list of blocks (`text`, `thinking`, `tool_use`, `tool_result`). Real user prompts are `user` records whose content is a string or text blocks, not `isMeta`, not `tool_result`. Slash commands appear as `<command-message>`/`<command-name>` strings. `tool_use` blocks for `Write` carry `file_path` + `content`; `Edit` carries `old_string` + `new_string`. Subagent records have `isSidechain: true`. The format is internal to Claude Code and undocumented.
- **Skill features (Claude Code 2.1.331)**: frontmatter `context: fork` runs the skill in a subagent; `background` (default on for forks) reports back as a task notification without blocking; `model` is honored for forked skills; `agent` picks the agent type. Skill text supports `${CLAUDE_SESSION_ID}` and `${CLAUDE_SKILL_DIR}` substitution. Whether a forked skill inherits conversation history is not documented, so the design does not depend on it.
- **Name clash**: Claude Code has a built-in `/recap` ("one-line session recap"); the skill is named `/listen`.

## Goals / Non-Goals

**Goals:**
- Invoking `/listen` costs the main session one command line and one short notification — nothing else.
- Deterministic, testable scope selection that does not depend on what survives compaction.
- One cold-start process per invocation; no daemon to manage.
- Quantization chosen from measured data on this machine, and changeable via config.

**Non-Goals:**
- Real-time / start-speaking-within-seconds playback (not achievable on this hardware at acceptable quality; revisit after the benchmark).
- Cross-platform playback (macOS `afplay` only).
- Reusing `~/.venv-vllm-metal`.

## Decisions

### D1. Forked background skill that reads the transcript itself

The skill uses `context: fork`, `background: true`, `model: sonnet`, and `disable-model-invocation: true` (only the user triggers it). The subagent gets the session id via `${CLAUDE_SESSION_ID}` and reads the transcript from disk.

```
 MAIN SESSION            FORKED SUBAGENT (background, sonnet)        DETACHED PROCESS
 /listen 3  --spawn-->   1. extract  --session $ID --scope 3  -> digest
 (keeps working)         2. write solo script (style guide)   -> <ts>.txt
                         3. speak <ts>.txt  (detach) ----------------> load Orpheus
                         4. return one line                           synthesize chunks
 <-- "Listen: last 3 turns, ~2 min, 8bit; plays when ready"           join -> <ts>.wav
                                                                      notify + afplay
```

Alternatives considered:
- *Inline skill* — the transcript digest and script would land in the main context. Rejected: that is exactly what the user wants to avoid.
- *Agent-tool fork that inherits the conversation* — no parsing needed, but after compaction "whole session" only sees the summary, it depends on an undocumented inheritance behavior, and it re-reads the whole main context. Rejected.
- *Stop hook that always speaks* — the user wants on-demand only.

Model: Sonnet is enough for a ~300-word spoken summary and is cheaper than the session model. It is a one-line frontmatter change if quality disappoints.

### D2. Deterministic transcript extractor

A script turns the transcript into a plain-text digest for a given scope.

- **Turn** = one real user prompt plus every assistant record until the next real user prompt.
- **Excluded**: `isMeta` records, `isSidechain` records, `thinking` blocks, `tool_result` content, non-message record types (attachments, snapshots, request blobs), any turn whose prompt is a `/listen` invocation, and task notifications produced by previous `/listen` runs. The trailing in-progress `/listen` turn is never part of the scope.
- **Kept per turn**: the user prompt; assistant `text` blocks; for `Write`, the file path and full content; for `Edit`, the file path and the new text; for other tool calls, a one-line marker (`[ran Bash: <description>]`) so the narrative stays coherent without raw output.
- **Scopes**: `last` = last complete turn; `N` = last N complete turns (fewer if the session has fewer, reported in the digest header); `all` = every turn, including those before compaction, because the file keeps them.

Size budget (default ~60k characters of digest), applied oldest-first until it fits:
1. Replace file contents in older turns with path + first lines.
2. Reduce older turns to prompt + final assistant text only.
3. Drop the oldest turns, leaving a "N earlier turns omitted" line.

The most recent turn is never condensed. The digest header records whether condensing happened so the script can say so.

Alternative considered: multi-pass map-reduce summarization of huge sessions. Rejected for v1: more model calls and latency for an edge case; the tiered budget is deterministic and testable.

### D3. Script writing rules

The subagent writes a single-narrator explainer, following a style guide shipped with the skill:

- **Shape**: what this is → why it matters → how it works → trade-offs or open questions → one-sentence takeaway.
- **Length**: default ~300 words (~2 minutes at ~150 wpm), configurable. The budget scales down for `last` when there is little content.
- **Written for the ear**: short sentences; no markdown, lists, tables, code or ASCII diagrams; file paths and identifiers paraphrased ("the proposal file", "the extract script"); numbers and acronyms written the way they should be spoken; signposting ("three things matter here").
- **Orpheus tags**: at most two of `<chuckle>`, `<sigh>`, etc. per script, only where natural.
- **Honesty**: if the digest was condensed or N exceeded the available turns, say so in one sentence.

Output is plain UTF-8 text saved as `<ts>.txt`.

### D4. Speech runner: cold start, chunked synthesis, generate-then-play

A separate script loads the configured Orpheus build through the `mlx-audio` Python API and synthesizes the script.

- **Chunking**: split on sentence boundaries into chunks of one to three sentences (≤ ~300 characters); Orpheus drifts on long inputs.
- **Runaway guard**: per-chunk `max_tokens` proportional to chunk length (≈ 82 tokens/s × estimated seconds × 1.5 margin). A chunk that hits the cap is retried once, then the run fails with a clear reason rather than playing garbage.
- **Assembly**: chunks are joined with a short silence into one 24 kHz WAV.
- **Playback**: v1 generates the whole file, then plays it. With an estimated real-time factor below 1, playing while generating would leave gaps. If the benchmark shows a real-time factor ≥ ~1.0 for the chosen quant, buffered early start becomes a follow-up.
- **Cold start per invocation**: model load (~5–15 s) is small next to minutes of generation, and nothing stays resident when idle.

Alternatives considered: `mlx_audio.server` or a custom warm daemon (faster first audio, but holds several GB permanently and adds lifecycle management); shelling out to the `mlx_audio.tts.generate` CLI (less control over chunking, guards and progress). The CLI is still used for the quick benchmark sanity check.

### D5. Detached process, single player, stop

- The subagent launches the runner fully detached (new session, output to a log) and returns right away; it never waits on synthesis, so Bash timeouts do not matter.
- A state directory holds a pid file for the active runner/player. Starting a new run first stops any active one (single player).
- `/listen stop` takes the same skill path with argument `stop`: the subagent runs the runner's stop command, which kills the generator and `afplay` and clears the pid file.
- macOS notifications (`osascript display notification`) are sent when playback starts and on failure. They are out-of-band, so they cost the main session nothing.

### D6. Isolated, pinned Python environment

The scripts declare their dependencies inline (PEP 723) and run via `uv run`, with `mlx-audio` pinned to a tested version. This keeps them independent of `~/.venv-vllm-metal`. Model weights live in the normal Hugging Face cache and are pre-fetched at install time, including SNAC, so the first `/listen` does not stall on a download and speaking never needs the network.

### D7. Configuration and outputs

- Config file `~/.config/voice-cc/listen.toml` with keys `quant` (`bf16|8bit|6bit|4bit`), `voice` (default `tara`), `words` (default 300), `digest_chars` (default 60000), `keep` (default 20). Environment variables override the file.
- Outputs go to `~/.local/share/voice-cc/listen/<timestamp>-<session-short-id>.{txt,wav,log}`. Only the most recent `keep` runs are kept.

### D8. Quantization benchmark method

- **Reference script**: one fixed script of about 60 seconds covering plain narration, numbers and versions, acronyms, one long compound sentence, and two emotion tags.
- **Per build** (`bf16`, `8bit`, `6bit`, `4bit`): cold load time, generation tokens/s, real-time factor, time to first chunk, peak memory, plus count of runaway or failed chunks. Three runs each; report the median.
- **Listening**: samples renamed to random labels for blind A/B listening, scored for clarity, prosody and glitches.
- **Decision rule**: the default is the lowest-bit build with no audible glitches and clarity judged equal to bf16. Ties go to the faster build.
- Results are recorded in `bench/RESULTS.md` with the machine, versions and date. That file sets the defaults for `quant` and, if needed, `words`.

### D9. Repository layout and install

```
voice-cc/
  skills/listen/
    SKILL.md              # frontmatter + subagent instructions
    style-guide.md        # D3 rules, read by the subagent
    scripts/extract.py    # D2
    scripts/speak.py      # D4/D5 (run | stop | status)
  bench/
    reference.txt
    bench_quant.py
    RESULTS.md
  install.sh              # symlink + prefetch
```

`install.sh` symlinks `~/.claude/skills/listen` to `skills/listen` and pre-fetches the weights. `SKILL.md` refers to scripts through `${CLAUDE_SKILL_DIR}`, so the repo can live anywhere. `allowed-tools` grants only `Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/*)`-style commands plus `Read`, so the background subagent does not stop on permission prompts.

## Risks / Trade-offs

- [Claude Code changes forked-skill frontmatter or transcript layout] → The extractor fails loudly with a one-line reason instead of producing an empty script. Fixture-based tests run against a sample transcript, and the README states the minimum tested version (2.1.331).
- [Generation is slower than real time; a 2-minute recap may take several minutes] → The background flow plus a notification means you are not waiting at the prompt. The benchmark sets the quant, and `words` can be lowered.
- [4-bit or 6-bit audio artifacts or looping] → Per-chunk token cap with retry, the blind listening gate in D8, and the quant stays configurable.
- [Memory pressure when other local models are loaded, e.g. Ollama] → The default is set by the benchmark (likely 8-bit or lower, about 3.5 GB or less). The runner logs peak memory.
- [Permission prompts stall a background subagent] → Narrow `allowed-tools` for the exact script invocations. The end-to-end task verifies a run with no prompts.
- [Session content written to disk as scripts and audio] → Stored under the user's home directory and pruned to the most recent `keep` runs. Synthesis is local. The only remote processing is the subagent's model call, the same provider as the session itself.
- [Concurrent Claude Code sessions] → The transcript is chosen by exact session id, so there is no "newest file" guessing. One global player means the latest `/listen` wins.
- [Mispronounced identifiers or jargon] → The style guide paraphrases identifiers. Remaining issues are tuned through the style guide, not code.

## Migration Plan

- **Install**: run `install.sh` (creates the symlink, sets up the uv environment, pre-fetches models), then run `/listen` in any session.
- **Rollback**: remove the `~/.claude/skills/listen` symlink. Optionally delete `~/.local/share/voice-cc/listen/`, `~/.config/voice-cc/` and the cached Orpheus weights.

## Open Questions

- Default voice: `tara` is the starting point. Picking another (`leah`, `leo`, …) after listening is a config change only.
- Exact default word budget: 300 is provisional and may be lowered once real generation times are known (config only).
