# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["mlx-audio==0.6.8"]
# ///
"""Benchmark Orpheus 3B builds on this machine (design D8).

  uv run --script bench/bench_quant.py [--builds bf16,8bit,6bit,4bit] [--runs 3] [--asr]

Each run is a fresh process (cold start, as /listen does) that synthesizes
bench/reference.txt with the same chunking and runaway guard as speak.py.
Builds are interleaved per round to spread thermal effects. Prints a table of
medians, writes bench/results.json, and writes blind samples to bench/samples/
with random labels; the label key is bench/samples/.key.json (do not peek).
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent / "skills" / "listen" / "scripts"))
import listen_config  # noqa: E402
import speak  # noqa: E402

BUILDS = ("bf16", "8bit", "6bit", "4bit")
ASR_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
METRICS = [  # key, header, format
    ("load_s", "cold load s", "{:.1f}"),
    ("tok_s", "tokens/s", "{:.1f}"),
    ("rtf", "× real time", "{:.2f}"),
    ("ttfc_s", "first chunk s", "{:.1f}"),
    ("gen_s", "generate s", "{:.0f}"),
    ("audio_s", "audio s", "{:.1f}"),
    ("wpm", "words/min", "{:.0f}"),
    ("peak_gb", "peak GB", "{:.1f}"),
    ("runaways", "runaway chunks", "{:.0f}"),
    ("failures", "short chunks", "{:.0f}"),
]


def one_run(quant: str, text_path: Path, wav_path: Path) -> dict:
    """Runs inside a fresh process: load, synthesize, write the WAV, report metrics."""
    started = time.perf_counter()
    config = listen_config.Config(quant=quant, voice="tara", words=300, digest_chars=60000, keep=20)
    chunks = speak.chunk_text(text_path.read_text(encoding="utf-8"))
    words = sum(c.words for c in chunks)
    first_chunk = {}

    def on_chunk(i, n, audio, seconds):
        if i == 1:
            first_chunk["s"] = seconds

    result = {"quant": quant, "chunks": len(chunks), "words": words}
    synth = speak.Synthesizer(config, log=lambda line: print(line, file=sys.stderr))
    result["load_s"] = synth.load_seconds
    gen_start = time.perf_counter()
    try:
        audio = synth.synthesize(chunks, on_chunk=on_chunk)
    except speak.ListenError as exc:
        result.update(error=str(exc), runaways=synth.runaways, failures=synth.failures)
        return result
    gen_s = time.perf_counter() - gen_start
    speak.write_wav(wav_path, audio)
    audio_s = len(audio) / speak.SAMPLE_RATE
    result.update(
        gen_s=gen_s,
        audio_s=audio_s,
        tokens=synth.tokens,
        tok_s=synth.tokens / gen_s,
        rtf=audio_s / gen_s,
        ttfc_s=synth.load_seconds + first_chunk.get("s", 0.0),
        wpm=words / (audio_s / 60),
        peak_gb=synth.peak_memory_gb,
        runaways=synth.runaways,
        failures=synth.failures,
        total_s=time.perf_counter() - started,
        wav=str(wav_path),
    )
    return result


def normalize(text: str) -> list[str]:
    text = re.sub(r"<[a-z]+>", " ", text.lower())
    text = re.sub(r"(\d)\.(\d)", r"\1 point \2", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return text.split()


def wer(reference: list[str], hypothesis: list[str]) -> float:
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, 1):
        current = [i] + [0] * len(hypothesis)
        for j, hyp_word in enumerate(hypothesis, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref_word != hyp_word))
        previous = current
    return previous[-1] / max(1, len(reference))


def transcribe(paths: list[str]) -> dict[str, str]:
    from mlx_audio.stt.utils import load

    model = load(ASR_MODEL)
    return {p: model.generate(p).text.strip() for p in paths}


def versions() -> dict:
    import importlib.metadata as md

    found = {name: md.version(name) for name in ("mlx-audio", "mlx", "transformers")}
    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    memory = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip() or 0)
    found.update(machine=chip, memory_gb=round(memory / 2**30), macos=platform.mac_ver()[0], python=platform.python_version())
    return found


def table(rows: dict[str, dict]) -> str:
    headers = ["build"] + [h for _, h, _ in METRICS] + (["WER"] if any("wer" in r for r in rows.values()) else [])
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for build, row in rows.items():
        cells = [build] + [fmt.format(row[key]) if key in row else "—" for key, _, fmt in METRICS]
        if "WER" in headers:
            cells.append(f"{row['wer']:.1%}" if "wer" in row else "—")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--builds", default=",".join(BUILDS))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--text", type=Path, default=BENCH / "reference.txt")
    parser.add_argument("--out", type=Path, default=BENCH / "samples")
    parser.add_argument("--asr", action="store_true", help=f"also score intelligibility with {ASR_MODEL}")
    parser.add_argument("--one", nargs=2, metavar=("QUANT", "WAV"), help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.one:
        print(json.dumps(one_run(args.one[0], args.text, Path(args.one[1]))))
        return 0

    builds = [b.strip() for b in args.builds.split(",") if b.strip()]
    unknown = [b for b in builds if b not in BUILDS]
    if unknown:
        parser.error(f"unknown build(s): {', '.join(unknown)}")
    raw_dir = args.out / "raw"
    if args.out.exists():
        shutil.rmtree(args.out)
    raw_dir.mkdir(parents=True)

    runs: list[dict] = []
    for round_no in range(1, args.runs + 1):
        for build in builds:
            wav = raw_dir / f"{build}-run{round_no}.wav"
            print(f"[{datetime.now():%H:%M:%S}] round {round_no}/{args.runs}: {build} ...", flush=True)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--text", str(args.text), "--one", build, str(wav)],
                capture_output=True, text=True,
            )
            try:
                result = json.loads(proc.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError):
                result = {"quant": build, "error": (proc.stderr.strip().splitlines() or ["crashed"])[-1]}
            result["round"] = round_no
            runs.append(result)
            summary = result.get("error") or (f"{result['audio_s']:.1f}s audio in {result['gen_s']:.0f}s, "
                                              f"{result['tok_s']:.1f} tok/s, peak {result['peak_gb']:.1f} GB")
            print(f"    {summary}", flush=True)

    if args.asr:
        print(f"[{datetime.now():%H:%M:%S}] scoring intelligibility with {ASR_MODEL} ...", flush=True)
        reference = normalize(args.text.read_text(encoding="utf-8"))
        texts = transcribe([r["wav"] for r in runs if "wav" in r])
        for r in runs:
            if r.get("wav") in texts:
                r["transcript"] = texts[r["wav"]]
                r["wer"] = wer(reference, normalize(texts[r["wav"]]))

    medians: dict[str, dict] = {}
    for build in builds:
        ok = [r for r in runs if r["quant"] == build and "error" not in r]
        row = {key: statistics.median(r[key] for r in ok) for key, _, _ in METRICS if ok}
        row["runaways"] = sum(r.get("runaways", 0) for r in runs if r["quant"] == build)
        row["failures"] = sum(r.get("failures", 0) for r in runs if r["quant"] == build)
        row["failed_runs"] = sum("error" in r for r in runs if r["quant"] == build)
        if ok and all("wer" in r for r in ok):
            row["wer"] = statistics.median(r["wer"] for r in ok)
        medians[build] = row

    # Blind samples: shuffle labels so the listener cannot tell builds apart.
    labels = random.sample("ABCDEFGH"[: len(builds)], len(builds))
    key = dict(zip(labels, builds))
    for label, build in sorted(key.items()):
        for r in runs:
            if r["quant"] == build and "wav" in r:
                shutil.copyfile(r["wav"], args.out / f"{label}{r['round']}.wav")
    (args.out / ".key.json").write_text(json.dumps(key, indent=2))
    shutil.rmtree(raw_dir)
    for r in runs:
        r.pop("wav", None)

    report = {"date": datetime.now().isoformat(timespec="seconds"), "versions": versions(),
              "text": str(args.text.name), "runs_per_build": args.runs, "medians": medians, "runs": runs}
    (BENCH / "results.json").write_text(json.dumps(report, indent=2))
    print()
    print(table(medians))
    failed = {b: m["failed_runs"] for b, m in medians.items() if m["failed_runs"]}
    if failed:
        print(f"\nfailed runs: {failed}")
    print(f"\nblind samples: {args.out}/<label><run>.wav  (labels {', '.join(sorted(key))}; key in .key.json)")
    print(f"raw results: {BENCH / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
