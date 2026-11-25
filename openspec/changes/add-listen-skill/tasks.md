## 1. Environment and quantization benchmark

- [x] 1.1 Create the repo layout from design D9 (`skills/listen/scripts/`, `bench/`). Verify with `ls`.
- [x] 1.2 Write the PEP 723 header with a pinned `mlx-audio` and confirm `uv run` resolves it without touching `~/.venv-vllm-metal`. Verify: a one-line `uv run` import check prints the `mlx-audio` version.
- [x] 1.3 Download `mlx-community/orpheus-3b-0.1-ft-{bf16,8bit,6bit,4bit}` and `mlx-community/snac_24khz` into the Hugging Face cache. Verify each loads and speaks one sentence with voice `tara` (one WAV per build).
- [x] 1.4 Write `bench/reference.txt`: about 60 seconds of narration with numbers and versions, acronyms, one long compound sentence, and two emotion tags. Verify: word count is 140–160.
- [x] 1.5 Implement `bench/bench_quant.py` (design D8). For each build, measure cold load time, tokens/s, real-time factor, time to first chunk, peak memory and runaway chunks over 3 runs (report the median), and write randomly labeled samples plus a hidden label key. Verify: one run prints a full metrics table for all four builds.
- [x] 1.6 Run the blind listening test and record metrics, scores, machine, versions and date in `bench/RESULTS.md`. Choose the default `quant` (and `words` if generation time requires) using the D8 decision rule. Verify: RESULTS.md names the chosen default and the reason.

## 2. Transcript extractor

- [x] 2.1 Build test fixtures from real `.jsonl` transcripts, with session details removed or anonymized. Cover: plain turns, tool-heavy turns, a `Write` of an OpenSpec artifact, an `Edit`, sidechain and meta records, earlier `/listen` turns and notifications, and a compacted session. Verify: fixtures exist under `skills/listen/tests/fixtures/`.
- [x] 2.2 Implement transcript lookup by session id across `~/.claude/projects/*/<session-id>.jsonl`, with a clear one-line error when not found. Verify with a unit test for found, not found, and the same id under multiple project folders.
- [x] 2.3 Implement turn segmentation and the exclusion rules from design D2: meta, sidechain, thinking, tool results, `/listen` turns and notifications, and the in-progress turn. Verify: unit tests on fixtures assert turn counts and that excluded content is absent.
- [x] 2.4 Implement the kept content per turn: prompt, assistant text, `Write` path and content, `Edit` path and new text, and one-line markers for other tools. Verify: the OpenSpec `Write` fixture's digest contains the proposal body.
- [x] 2.5 Implement the scopes `last`, `N` and `all`, including the "only K turns available" header when N exceeds the count. Verify with unit tests for each scope, N > count, and `all` on the compacted fixture including pre-compaction turns.
- [x] 2.6 Implement the tiered size budget (design D2) with the `condensed` header flag. Verify: a synthetic oversized fixture fits within `digest_chars`, keeps the latest turn intact, and sets the flag.

## 3. Speech runner

- [x] 3.1 Implement config loading (`~/.config/voice-cc/listen.toml`, environment overrides, and defaults from 1.6). Verify: unit tests for file, environment override, and missing-file defaults.
- [x] 3.2 Implement sentence chunking (1–3 sentences, at most ~300 characters) that keeps Orpheus emotion tags intact. Verify: unit tests for abbreviations, decimals, long sentences, and tags.
- [x] 3.3 Implement synthesis via the `mlx-audio` Python API: per-chunk `max_tokens` guard, one retry, abort on a second runaway, and chunks joined with short silences into one 24 kHz WAV. Verify: `speak.py run bench/reference.txt` produces a WAV whose duration is within ±20% of the expected length.
- [x] 3.4 Implement detached `run`: new session, log file, pid/state file, stop any active run first. Also implement `stop` and `status`. Verify: run, then stop within 2 s; stop with nothing active reports "nothing to stop"; a second run replaces the first.
- [ ] 3.5 Implement playback with `afplay` after synthesis completes, plus macOS notifications on playback start and on failure. Verify: hear the audio and see both notification types (force a failure with a missing model build).
- [x] 3.6 Implement output naming (`<timestamp>-<session-short-id>.{txt,wav,log}`) and retention pruning to `keep`. Verify: with `keep=2`, three runs leave only the newest two sets of files.

## 4. Skill definition

- [x] 4.1 Write `skills/listen/style-guide.md` from design D3: shape, word budget, written for the ear, tag limit, and the honesty line for condensed or short scopes. Verify: review against the spec's "Solo spoken explainer" scenarios.
- [x] 4.2 Write `skills/listen/SKILL.md` frontmatter: `context: fork`, `background: true`, `model: sonnet`, `disable-model-invocation: true`, an argument hint, and narrow `allowed-tools` for the two scripts plus `Read`. Verify: the skill appears in Claude Code and is not auto-invoked.
- [x] 4.3 Write the SKILL.md body: parse `last|N|all|stop`, return a one-line usage error on invalid input, run the extractor with `${CLAUDE_SESSION_ID}`, write the script per the style guide to the output path, launch `speak.py run` detached, and return a single line (scope, estimated duration, quant). `stop` calls `speak.py stop` directly. Verify: an invalid argument returns only the usage line.

## 5. Install and end-to-end verification

- [x] 5.1 Write `install.sh`: symlink `~/.claude/skills/listen` to `skills/listen`, warm the uv environment, and prefetch the default model build and SNAC. It must be safe to re-run. Verify: run it twice, and `/listen` is available in a different project.
- [ ] 5.2 End-to-end, after an `/opsx:propose` run: `/listen` speaks about the proposal content, the main session gains only the command and one notification of at most two lines, and there are no permission prompts. Verify by inspecting the main session transcript.
- [ ] 5.3 End-to-end scopes: `/listen 3`, `/listen 50` (reports fewer turns available), and `/listen all` in a compacted session (covers pre-compaction topics). Verify by listening and reading the saved scripts.
- [ ] 5.4 End-to-end control: send a new prompt while audio is generating (it is handled normally), run `/listen stop` during playback (it stops), and start a new `/listen` during playback (the old one is replaced). Verify by observation.
- [x] 5.5 Offline check: with the network disabled, re-synthesize a saved script. Verify that it plays.
- [x] 5.6 Write `README.md` covering usage, config keys, output location, benchmark results summary, the minimum tested Claude Code version (2.1.331) and rollback steps. Verify: a fresh reader can install and run `/listen` from it alone.
