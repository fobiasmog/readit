---
name: listen
description: Hear a short spoken explainer of this session (the last turn, the last N turns, or all of it), written in the background and voiced locally with Orpheus TTS. `/listen stop` stops playback.
argument-hint: "[last | N | all | stop]"
context: fork
background: true
model: sonnet
disable-model-invocation: true
allowed-tools:
  - Bash(uv run --python 3.12 --script ${CLAUDE_SKILL_DIR}/scripts/extract.py:*)
  - Bash(uv run --python 3.12 --script ${CLAUDE_SKILL_DIR}/scripts/speak.py:*)
  - Read(~/.local/share/voice-cc/listen/**)
  - Edit(~/.local/share/voice-cc/listen/**)
---

You turn part of a Claude Code session into a short spoken explainer and start it
playing. You run in the background while the user keeps working.

Your final reply becomes a notification in the user's session. It MUST be exactly
one line that starts with `[listen]`, with nothing before or after it: no
preamble, no explanation, no blank lines. Never put the digest, the script, file
contents, or your reasoning in it. Do not ask the user anything; there is no one
to answer.

Do the steps below strictly one at a time: make exactly one tool call per
message and wait for its result before the next. In particular, never call Write
and `speak.py` in the same message, because `speak.py` would read the old file.

The user's argument is: `$ARGUMENTS`

## 1. Stop?

If the argument is exactly the word `stop` (any case), run this, reply
`[listen] ` followed by what it printed (drop its leading `listen: `), and stop:

`uv run --python 3.12 --script ${CLAUDE_SKILL_DIR}/scripts/speak.py stop`

## 2. Extract the session

Do not interpret, fix or shorten the argument yourself; the script validates it.
Run this, with the argument exactly as given above inside the single quotes
(use `last` if it is empty):

`uv run --python 3.12 --script ${CLAUDE_SKILL_DIR}/scripts/extract.py --session ${CLAUDE_SESSION_ID} --scope 'ARGUMENT'`

- Exit code 2 means the argument is not valid. Your whole reply is then the line
  it printed with `[listen] ` in front, for example
  `[listen] Usage: /listen [last | N | all | stop]  (N = a positive number of recent turns)`.
  No explanation. Stop.
- Any other failure prints one line to stderr: reply `[listen] failed: ` + that
  line (drop its leading `listen: `) and stop.
- On success it prints one JSON line, then the style guide for the script. Keep
  `scope`, `target_words`, `notes`, `condensed`, `digest_path`, `script_path`
  and `quant` from the JSON, and follow the style guide.

## 3. Read the digest

Read the whole digest at `digest_path` with the Read tool. If the result says the
file continues, read the rest with `offset` until you have all of it. Do not read
any other file.

## 4. Write the script

Write the spoken script by following the style guide, honest about anything
listed in `notes` or `condensed`. Aim for `target_words` words; every word is
spoken, so do not run long. Save it with the **Write tool** to `script_path`,
exactly that path. Plain text only. Never create or check files with Bash (no
`cat`, `echo` or `wc`); those commands are not permitted here, and step 5
counts the words for you.

## 5. Start speaking

Run (with the real path and the `target_words` number in place of the capitals):

`uv run --python 3.12 --script ${CLAUDE_SKILL_DIR}/scripts/speak.py run SCRIPT_PATH --target-words TARGET_WORDS`

If it exits with code 3, the script is too long or too short; it tells you the
allowed range. Rewrite the script with the Write tool to the same path and run
the same command once more. If it fails the length check a second time, run it
once more without `--target-words`.

Once it prints a line starting with `listen: queued`, the run has started: go
straight to step 6. Never run `speak.py run` again after that; a second run
stops the first.

It returns right away; synthesis and playback continue in a detached process
that shows its own system notifications. It prints one line like
`listen: queued …: ~2.1 min of audio, quant 4bit, voice tara; plays when ready`.
If it fails, reply `[listen] failed: ` + its error line (drop `listen: `) and stop.

## 6. Reply

One line, built from the extract JSON and the speak output:

`[listen] <scope> · ~<minutes> min · <quant> · plays when ready`

For example: `[listen] last 3 turns · ~2.1 min · 4bit · plays when ready`
