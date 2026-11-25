# Orpheus quantization benchmark

- **Date:** 2026-11-25
- **Machine:** Apple M2 Pro, 32 GB unified memory, macOS 26.7.3
- **Software:** mlx-audio 0.6.8, mlx 0.32.5, transformers 5.24.1, Python 3.11.3
- **Models:** `mlx-community/orpheus-3b-0.1-ft-{bf16,8bit,6bit,4bit}`, voice `tara`,
  SNAC 24 kHz codec
- **Raw data:** [results.json](results.json). Blind samples: `samples/` (not committed).

## Method (design D8)

- **Input:** [reference.txt](reference.txt), 158 words with numbers and versions,
  acronyms, one long compound sentence and two emotion tags. The chunker splits it
  into 7 chunks, the same way `/listen` would.
- **Runs:** 3 per build. Each run is a fresh process, a cold start like a real
  `/listen`, using `speak.py`'s own chunking, token cap, retry and silence
  trimming. Builds are interleaved per round to spread thermal effects. The
  table shows medians.
- **Columns:**
  - "Cold load": loading weights into memory. The files were already in the OS
    page cache, as they will be after the first use.
  - "First chunk": load plus the first chunk's generation, i.e. how soon audio
    could start if it were streamed.
  - "× real time": seconds of audio produced per second of generation.
- **Intelligibility:** each output was transcribed with
  `mlx-community/parakeet-tdt-0.6b-v3` and compared with the reference
  (word error rate, WER).

## Results (median of 3 runs)

| build | cold load s | tokens/s | × real time | first chunk s | generate s | audio s | words/min | peak GB | runaway chunks | short chunks | WER |
|---|---|---|---|---|---|---|---|---|---|---|---|
| bf16 | 3.0 | 25.0 | 0.28 | 35.0 | 235 | 67.3 | 141 | 8.3 | 0 | 0 | 1.2% |
| 8bit | 2.9 | 45.6 | 0.52 | 22.3 | 131 | 68.2 | 139 | 5.2 | 0 | 0 | 0.6% |
| 6bit | 2.4 | 57.9 | 0.66 | 17.5 | 104 | 69.0 | 137 | 4.4 | 0 | 0 | 1.8% |
| 4bit | 2.3 | 78.7 | 0.89 | 12.5 | 75 | 66.8 | 142 | 3.5 | 0 | 0 | 0.6% |

**What a default-length explainer costs:** 300 words is about 2.1 minutes of
speech at the measured ~140 words per minute. The time until it starts playing,
after the script is written:

| build | wait before audio | peak memory |
|---|---|---|
| bf16 | ~7.7 min | 8.3 GB |
| 8bit | ~4.2 min | 5.2 GB |
| 6bit | ~3.3 min | 4.4 GB |
| 4bit | ~2.5 min | 3.5 GB |

No build reaches real time (× ≥ 1.0), so generate-then-play (design D4) stays.
Streaming playback would stutter.

## Intelligibility details

Word differences from the reference, per run. "cache" → "cash" is left out: it
is the correct pronunciation and the transcriber spells it as the homophone.
"ugh"/"tss" are the `<sigh>`/`<chuckle>` sounds.

| build | run 1 | run 2 | run 3 |
|---|---|---|---|
| bf16 | final sentence missing from transcript (12 words)¹ | — | — |
| 8bit | — | "billing"→"boeing", "step"→"set" | — |
| 6bit | "There is one catch." missing² | "a"→"the" | "column"→"call", "it's"→"its" |
| 4bit | — | — | "cached"→"cache" |

¹ The audio is a normal length (66.0 s against 67.3 s and 68.2 s for the other
bf16 runs). A missing 12-word sentence would be about 5 s shorter, so this is
most likely the transcriber cutting the tail. Check it by ear.
² Probably a real skip: Orpheus sometimes swallows a short sentence that opens a
chunk ahead of a long clause. An early 8-bit test showed the same thing. Check
it by ear.

Single-word differences are within the transcriber's own noise. The WER column
is a sanity check, not a ranking.

**Earlier finding that changed the code:** before chunk rebalancing, a chunk
holding only `<chuckle> Nobody expected that.` made the 4-bit build run past
its token cap twice in a row, and the guard aborted the run. The 8-bit build
needed one retry on a similar 4-word chunk. `speak.py` now merges lone short
sentences with a neighbouring sentence. Since then there have been 0 runaways
in 12 runs plus 5 short test runs.

## Listening test

Samples are in `bench/samples/`: `A1`–`D3`, one letter per build, three runs
each. The label key is `bench/samples/.key.json`.

**What happened (2026-11-25):** the user asked which label was 4-bit before
scoring, so **D (4bit) was heard unblinded**. A–C stayed blind and were not
scored. Verdict on D: "sounds good", with no glitches reported.

To repeat the test properly: score A–D on clarity and prosody (1–5), note any
glitches, and only then open the key.

```sh
cd bench/samples && for f in A1 B1 C1 D1; do echo $f; afplay $f.wav; done
```

## Decision

**Rule (D8):** the default is the lowest-bit build with no audible glitches and
clarity judged equal to bf16. Ties go to the faster build.

**Default: `quant = "4bit"`.** It is the lowest-bit build, so if it passes,
nothing else needs testing. It passed on both counts:

- **By ear:** the user judged the 4-bit samples good, with no glitches heard.
  Caveat: this was a single unblinded listener, not a blind A/B against bf16.
- **Objectively:** it was as intelligible as bf16 to the transcriber (0.6% vs
  1.2% median WER; single-word differences only). It had no runaway or short
  chunks in 3 runs, plus 5 short test runs and a 102-second end-to-end
  explainer. It is 3.1× faster than bf16 (0.89× real time) and peaks at 3.5 GB
  instead of 8.3 GB.

**Risk to watch:** before chunk rebalancing, 4-bit was the build that looped on
a tiny tagged chunk. The runaway guard turns any recurrence into a clean
"Listen failed" notice rather than garbled audio. If that shows up in real use,
set `quant = "6bit"` (about 0.8 minutes more wait per explainer).

**`words` stays 300**: about 2.1 minutes of speech, and about 2.5 minutes of
generation on 4-bit, which the background flow absorbs.
