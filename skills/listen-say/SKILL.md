---
name: listen-say
description: Recap the last N turns of this conversation as a short spoken story and read it aloud with macOS say.
argument-hint: "[N]"
disable-model-invocation: true
allowed-tools: Bash(say:*)
---

Recap the last $ARGUMENTS turns of this conversation (if no number is given, the last turn).

Write it as a narrator telling the story: what the user wanted, what was done, what was found, and where things stand.
Plain spoken English, past tense, under 200 words. No markdown, no code, no file paths or commands read out; describe them instead.

Then run it **in the background** (run_in_background: true), with the text in a quoted heredoc:

```
say <<'TEXT'
<the recap>
TEXT
```

Reply with one short line saying it is being read aloud. Do not repeat the recap.
