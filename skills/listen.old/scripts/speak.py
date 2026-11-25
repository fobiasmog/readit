# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["mlx-audio==0.6.8"]
# ///
"""Synthesize a /listen script with Orpheus (mlx-audio) and play it in the background.

  speak.py run SCRIPT.txt [--foreground] [--no-play]   start a run (stops any active one)
  speak.py stop                                        stop the active run, if any
  speak.py status                                      show the active run, if any
  speak.py prefetch [--quant Q]                        download model files (install time)

`run` detaches by default: it starts a worker in a new session and returns at once.
The worker loads the model, synthesizes every chunk, writes one 24 kHz WAV, then
plays it with afplay. Nothing plays unless every chunk synthesized cleanly.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_config  # noqa: E402

SAMPLE_RATE = 24000
TOKENS_PER_SECOND = 82  # Orpheus: 7 SNAC codes per ~85 ms frame
TOKEN_MARGIN = 1.6
MIN_MAX_TOKENS = 400
WORDS_PER_SECOND = 140 / 60  # Orpheus tara, measured in bench/RESULTS.md
CHUNK_MAX_CHARS = 300  # mlx-audio splits anything longer itself
CHUNK_MAX_SENTENCES = 3
SHORT_CHUNK_CHARS = 60  # a lone sentence shorter than this is merged with a neighbour
PAUSE_SENTENCE = 0.35  # between chunks inside a paragraph
PAUSE_PARAGRAPH = 0.7
EDGE_KEEP = 0.08  # seconds of natural lead-in/out kept when trimming a chunk
STOP_GRACE = 1.5
WORDS_LOW, WORDS_HIGH = 0.85, 1.15  # accepted script length around --target-words
LENGTH_EXIT = 3
EMOTION_TAGS = ("laugh", "chuckle", "sigh", "cough", "sniffle", "groan", "yawn", "gasp")
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "approx",
    "fig", "inc", "ltd", "corp", "dept", "u.s", "u.k", "a.m", "p.m",
}
TAG = re.compile(r"<\s*([a-zA-Z]+)\s*>")
SENTENCE_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s)")


class ListenError(Exception):
    """Fatal problem; the message is one line, fit for a notification."""


# --------------------------------------------------------------------------- text → chunks


@dataclass
class Chunk:
    text: str
    pause_after: float = PAUSE_SENTENCE

    @property
    def words(self) -> int:
        return len(TAG.sub("", self.text).split())

    @property
    def tags(self) -> int:
        return len(TAG.findall(self.text))

    def expected_seconds(self) -> float:
        bare = TAG.sub("", self.text)
        by_words = len(bare.split()) / WORDS_PER_SECOND
        by_chars = len(bare) / 14.0
        return max(by_words, by_chars) + 0.8 * self.tags + 0.3

    def max_tokens(self) -> int:
        return max(MIN_MAX_TOKENS, math.ceil(TOKENS_PER_SECOND * self.expected_seconds() * TOKEN_MARGIN))


def clean_text(text: str) -> str:
    """Drop markup that should never be spoken; keep Orpheus emotion tags."""
    text = TAG.sub(lambda m: f"<{m.group(1).lower()}>" if m.group(1).lower() in EMOTION_TAGS else " ", text)
    text = re.sub(r"<(?!(?:%s)>)[^>\n]{0,80}>" % "|".join(EMOTION_TAGS), " ", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", text)


def split_sentences(paragraph: str) -> list[str]:
    sentences, start = [], 0
    for match in SENTENCE_END.finditer(paragraph):
        end = match.end()
        before = paragraph[start:match.start() + 1].split()
        last_word = before[-1].lower().rstrip(".") if before else ""
        if match.group().startswith(".") and len(match.group().rstrip("\"'”’)]")) == 1:
            if last_word in ABBREVIATIONS or re.fullmatch(r"[a-z]", last_word):
                continue  # "e.g.", "Dr.", an initial
        sentences.append(paragraph[start:end].strip())
        start = end
    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def split_long(sentence: str, limit: int = CHUNK_MAX_CHARS) -> list[str]:
    """Split one over-long sentence at clause boundaries, then at spaces."""
    pieces, rest = [], sentence.strip()
    while len(rest) > limit:
        window = rest[: limit + 1]
        cut = max((m.end() for m in re.finditer(r"[,;:—–]\s+|\s+-\s+", window) if m.end() <= limit), default=0)
        if cut < limit // 3:
            space = window.rfind(" ")
            cut = space if space > 0 else limit
        opened, closed = rest.rfind("<", 0, cut), rest.rfind(">", 0, cut)
        if opened > closed and opened > 0:
            cut = opened  # never cut inside an emotion tag
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def _fits(group: list[str], max_chars: int, max_sentences: int) -> bool:
    return len(group) <= max_sentences and len(" ".join(group)) <= max_chars


def rebalance(groups: list[list[str]], max_chars: int, max_sentences: int) -> list[list[str]]:
    """Avoid one-short-sentence chunks: Orpheus tends to ramble on very short input.

    A lone short sentence pulls the last sentence of the chunk before it, or
    else joins the chunk after it, as long as both limits still hold.
    """
    def is_short(group: list[str]) -> bool:
        return len(group) == 1 and len(TAG.sub("", group[0]).strip()) < SHORT_CHUNK_CHARS

    i = 0
    while i < len(groups):
        group = groups[i]
        short = is_short(group)
        before = groups[i - 1] if i > 0 else []
        if (short and len(before) >= 2 and not is_short(before[:-1])
                and _fits([before[-1]] + group, max_chars, max_sentences)):
            group.insert(0, before.pop())
        elif short and i + 1 < len(groups) and _fits(group + groups[i + 1], max_chars, max_sentences):
            groups[i + 1][:0] = group
            del groups[i]
            continue
        i += 1
    return groups


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS, max_sentences: int = CHUNK_MAX_SENTENCES) -> list[Chunk]:
    """1–3 sentences per chunk, at most max_chars, emotion tags kept whole."""
    chunks: list[Chunk] = []
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", clean_text(text))]
    for paragraph in (p for p in paragraphs if p):
        groups: list[list[str]] = [[]]
        for sentence in split_sentences(paragraph):
            for piece in split_long(sentence, max_chars):
                current = groups[-1]
                if current and (len(current) >= max_sentences or len(" ".join(current + [piece])) > max_chars):
                    groups.append(current := [])
                current.append(piece)
        groups = rebalance([g for g in groups if g], max_chars, max_sentences)
        chunks.extend(Chunk(" ".join(g)) for g in groups)
        if chunks:
            chunks[-1].pause_after = PAUSE_PARAGRAPH
    # A chunk that is only an emotion tag cannot be spoken alone; fold it forward.
    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and not TAG.sub("", merged[-1].text).strip():
            chunk = Chunk(f"{merged.pop().text} {chunk.text}", chunk.pause_after)
        merged.append(chunk)
    return merged


# --------------------------------------------------------------------------- synthesis


def model_snapshot(repo: str) -> Path:
    from huggingface_hub import snapshot_download

    try:
        return Path(snapshot_download(repo, local_files_only=True))
    except Exception:
        raise ListenError(f"listen: model {repo} is not installed; run install.sh") from None


class Synthesizer:
    """Loads one Orpheus build and turns chunks into audio, with a runaway guard."""

    def __init__(self, config: listen_config.Config, log=print):
        os.environ["HF_HUB_OFFLINE"] = "1"  # speaking never touches the network
        self.config = config
        self.log = log
        for repo in (config.model_repo, listen_config.SNAC_REPO):
            model_snapshot(repo)
        tokenizer = model_snapshot(listen_config.TOKENIZER_REPO)
        if not (tokenizer / "tokenizer.json").exists():
            raise ListenError(f"listen: tokenizer files for {listen_config.TOKENIZER_REPO} are missing; run install.sh")
        start = time.perf_counter()
        import mlx.core as mx
        from mlx_audio.tts.utils import load

        self.mx = mx
        self.model = load(config.model_repo)
        self.load_seconds = time.perf_counter() - start
        self.runaways = 0
        self.failures = 0
        self.tokens = 0

    def _attempt(self, chunk: Chunk):
        import numpy as np

        cap = chunk.max_tokens()
        parts, tokens, hit_cap = [], 0, False
        for result in self.model.generate(chunk.text, voice=self.config.voice, max_tokens=cap, split_pattern="\n\n\n"):
            parts.append(np.asarray(result.audio, dtype=np.float32))
            tokens += result.token_count
            hit_cap = hit_cap or result.token_count >= cap
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return audio, tokens, hit_cap, cap

    def chunk_audio(self, chunk: Chunk, index: int, total: int):
        for attempt in (1, 2):
            audio, tokens, hit_cap, cap = self._attempt(chunk)
            audio = trim_silence(audio)
            self.tokens += tokens
            seconds = len(audio) / SAMPLE_RATE
            too_short = seconds < 0.3 * chunk.expected_seconds()
            if not hit_cap and not too_short:
                return audio
            if hit_cap:
                self.runaways += 1
                problem = f"hit the {cap}-token cap"
            else:
                self.failures += 1
                problem = f"was too short ({seconds:.1f}s for ~{chunk.expected_seconds():.0f}s expected)"
            self.log(f"chunk {index}/{total} {problem} on attempt {attempt}: {chunk.text[:60]!r}")
        kind = "runaway generation" if hit_cap else "truncated audio"
        raise ListenError(f"listen: {kind} on chunk {index} of {total} after a retry; nothing played")

    def synthesize(self, chunks: list[Chunk], on_chunk=None):
        import numpy as np

        pieces = []
        for i, chunk in enumerate(chunks, 1):
            started = time.perf_counter()
            audio = self.chunk_audio(chunk, i, len(chunks))
            if on_chunk:
                on_chunk(i, len(chunks), audio, time.perf_counter() - started)
            pieces.append(audio)
            if i < len(chunks):
                pieces.append(np.zeros(int(SAMPLE_RATE * chunk.pause_after), dtype=np.float32))
            self.mx.clear_cache()
        return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)

    @property
    def peak_memory_gb(self) -> float:
        return self.mx.get_peak_memory() / 1e9


def trim_silence(audio, keep: float = EDGE_KEEP):
    """Cut leading and trailing silence (Orpheus pads chunks with up to ~2 s of it)."""
    import numpy as np

    frame = SAMPLE_RATE // 100  # 10 ms
    usable = len(audio) // frame * frame
    if usable == 0:
        return audio
    rms = np.sqrt(np.mean(np.square(audio[:usable].reshape(-1, frame)), axis=1))
    threshold = max(0.004, 0.05 * float(np.percentile(rms, 95)))
    voiced = np.flatnonzero(rms > threshold)
    if voiced.size == 0:
        return audio[:0]
    margin = int(keep * SAMPLE_RATE)
    start = max(0, voiced[0] * frame - margin)
    end = min(len(audio), (voiced[-1] + 1) * frame + margin)
    return audio[start:end]


def write_wav(path: Path, audio) -> None:
    """Write 16-bit mono WAV atomically, so a stopped run never leaves half a file."""
    import numpy as np

    tmp = path.with_suffix(".wav.part")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    tmp.replace(path)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


# --------------------------------------------------------------------------- outputs


STEM = re.compile(r"^(\d{8}-\d{6}-[A-Za-z0-9_-]+?)(?:\.digest\.md|\.txt|\.wav|\.wav\.part|\.log)$")


def new_stem(tag: str = "manual") -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{tag}"


def run_paths(script: Path) -> tuple[str, Path]:
    """Return (stem, script path inside the output dir), copying outside scripts in."""
    out = listen_config.output_dir()
    out.mkdir(parents=True, exist_ok=True)
    script = script.expanduser().resolve()
    match = STEM.match(script.name)
    if script.parent == out.resolve() and match:
        return match.group(1), script
    stem = new_stem()
    target = out / f"{stem}.txt"
    shutil.copyfile(script, target)
    return stem, target


def prune(keep: int, active: str | None = None) -> list[Path]:
    """Keep the newest `keep` runs (all files sharing a stem) and delete the rest."""
    out = listen_config.output_dir()
    if not out.is_dir():
        return []
    groups: dict[str, list[Path]] = {}
    for path in out.iterdir():
        match = STEM.match(path.name)
        if match and path.is_file():
            groups.setdefault(match.group(1), []).append(path)
    removed = []
    for stem in sorted(groups, reverse=True)[keep:]:
        if stem == active:
            continue
        for path in groups[stem]:
            with contextlib.suppress(OSError):
                path.unlink()
                removed.append(path)
    return removed


# --------------------------------------------------------------------------- state, notify, process control


def state_file() -> Path:
    return listen_config.state_dir() / "active.json"


@contextlib.contextmanager
def state_lock():
    directory = listen_config.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_state() -> dict | None:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return None


def write_state(state: dict) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def update_state(stem: str, **changes) -> None:
    with state_lock():
        state = read_state()
        if state and state.get("stem") == stem:
            state.update(changes)
            write_state(state)


def clear_state(stem: str) -> None:
    with state_lock():
        state = read_state()
        if state and state.get("stem") == stem:
            with contextlib.suppress(OSError):
                state_file().unlink()


def is_our_process(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "speak.py" in command and "_worker" in command


def notify(message: str, title: str = "Listen") -> None:
    if os.environ.get("VOICE_CC_NOTIFY", "1") == "0" or not shutil.which("osascript"):
        return
    script = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run"]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["osascript", *script, message[:200], title], timeout=5, capture_output=True)


def stop_active() -> str:
    """Stop the active worker and its afplay. Caller should hold state_lock()."""
    state = read_state()
    if not state:
        return "listen: nothing to stop"
    pid = int(state.get("pid", 0))
    stem = state.get("stem", "?")
    if not pid or not is_our_process(pid):
        with contextlib.suppress(OSError):
            state_file().unlink()
        return "listen: nothing to stop"
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)  # the worker leads its own session; afplay is in it
    deadline = time.monotonic() + STOP_GRACE
    while time.monotonic() < deadline and is_our_process(pid):
        time.sleep(0.05)
    if is_our_process(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        state_file().unlink()
    return f"listen: stopped {stem} ({state.get('phase', 'running')})"


# --------------------------------------------------------------------------- worker


def worker(script: Path, stem: str, play: bool = True, log=print) -> int:
    config = listen_config.load()
    out = listen_config.output_dir()
    wav_path = out / f"{stem}.wav"
    player: subprocess.Popen | None = None

    def on_term(signum, frame):
        # The stopper holds the state lock and cleans up; exit now without touching it.
        if player and player.poll() is None:
            player.terminate()
        with contextlib.suppress(Exception):
            log("stopped")
        os._exit(143)

    signal.signal(signal.SIGTERM, on_term)
    try:
        text = script.read_text(encoding="utf-8")
        chunks = chunk_text(text)
        if not chunks:
            raise ListenError("listen: the script is empty; nothing to say")
        words = sum(c.words for c in chunks)
        log(f"{stem}: {len(chunks)} chunks, {words} words, quant {config.quant}, voice {config.voice}")
        update_state(stem, phase="loading")
        synth = Synthesizer(config, log=log)
        log(f"model loaded in {synth.load_seconds:.1f}s")
        update_state(stem, phase="generating")
        started = time.perf_counter()

        def progress(i, n, audio, seconds):
            log(f"chunk {i}/{n}: {len(audio) / SAMPLE_RATE:.1f}s audio in {seconds:.1f}s")
            update_state(stem, progress=f"{i}/{n}")

        audio = synth.synthesize(chunks, on_chunk=progress)
        elapsed = time.perf_counter() - started
        write_wav(wav_path, audio)
        duration = len(audio) / SAMPLE_RATE
        log(f"wrote {wav_path.name}: {duration:.1f}s audio in {elapsed:.1f}s "
            f"({duration / elapsed:.2f}x real time, {synth.tokens / elapsed:.1f} tok/s, "
            f"peak {synth.peak_memory_gb:.1f} GB, {synth.runaways} runaway retries)")
        del synth
        if play and os.environ.get("VOICE_CC_PLAY", "1") == "0":
            log("playback skipped (VOICE_CC_PLAY=0)")
            play = False
        if play:
            update_state(stem, phase="playing")
            notify(f"Playing now: {duration / 60:.1f} min ({config.quant}, {config.voice})")
            player = subprocess.Popen(["afplay", str(wav_path)])
            player.wait()
            log(f"playback finished (exit {player.returncode})")
        return 0
    except ListenError as exc:
        log(str(exc))
        notify(str(exc).removeprefix("listen: "), title="Listen failed")
        return 1
    except Exception as exc:  # anything unexpected still gets a one-line notice
        log(traceback.format_exc())
        notify(f"{type(exc).__name__}: {exc}"[:180], title="Listen failed")
        return 1
    finally:
        clear_state(stem)
        for path in prune(config.keep, active=None):
            log(f"pruned {path.name}")


# --------------------------------------------------------------------------- CLI


def cmd_run(args) -> int:
    config = listen_config.load()  # fail fast on a bad config
    script = Path(args.script)
    if not script.is_file() or not script.read_text(encoding="utf-8").strip():
        raise ListenError(f"listen: script {script} is missing or empty")
    if args.target_words:
        words = sum(c.words for c in chunk_text(script.read_text(encoding="utf-8")))
        low, high = round(args.target_words * WORDS_LOW), round(args.target_words * WORDS_HIGH)
        if not low <= words <= high:
            print(f"listen: the script has {words} words but must have {low} to {high} "
                  f"(target {args.target_words}); rewrite it, save it to the same path, and run this again")
            return LENGTH_EXIT
    os.environ["HF_HUB_OFFLINE"] = "1"
    model_snapshot(config.model_repo)  # fail fast, naming the build, before detaching
    stem, script = run_paths(script)
    chunks = chunk_text(script.read_text(encoding="utf-8"))
    minutes = sum(c.words for c in chunks) / WORDS_PER_SECOND / 60
    log_path = listen_config.output_dir() / f"{stem}.log"

    if args.foreground:
        with contextlib.suppress(OSError), state_lock():
            stop_active()
        with log_path.open("a") as log_file:
            def log(line):
                stamped = f"{datetime.now():%H:%M:%S} {line}"
                print(stamped, flush=True)
                print(stamped, file=log_file, flush=True)
            return worker(script, stem, play=not args.no_play, log=log)

    with state_lock():
        previous = stop_active()
        log_handle = log_path.open("a")
        command = [sys.executable, str(Path(__file__).resolve()), "_worker", str(script), stem]
        if args.no_play:
            command.append("--no-play")
        # A stopped worker exits abruptly; keep the resource tracker's leak warning out of the log.
        env = {**os.environ, "PYTHONWARNINGS": "ignore:resource_tracker"}
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True, cwd=str(listen_config.output_dir()), env=env,
        )
        write_state({"pid": process.pid, "stem": stem, "phase": "starting", "started": time.time(),
                     "script": str(script), "log": str(log_path), "quant": config.quant})
    replaced = ""
    if previous.startswith("listen: stopped"):
        replaced = " (replaced the previous run)"
        # Only one run plays at a time; say so, or the older run just silently never plays.
        notify("A newer /listen replaced the one that was still running", title="Listen: previous run stopped")
    print(f"listen: queued {stem}: ~{max(minutes, 0.1):.1f} min of audio, quant {config.quant}, "
          f"voice {config.voice}; plays when ready{replaced}")
    return 0


def cmd_worker(args) -> int:
    def log(line):
        print(f"{datetime.now():%H:%M:%S} {line}", flush=True)
    return worker(Path(args.script), args.stem, play=not args.no_play, log=log)


def cmd_stop(args) -> int:
    with state_lock():
        print(stop_active())
    return 0


def cmd_status(args) -> int:
    state = read_state()
    if not state or not is_our_process(int(state.get("pid", 0))):
        print("listen: idle")
        return 0
    age = time.time() - float(state.get("started", time.time()))
    progress = f", chunk {state['progress']}" if state.get("progress") else ""
    print(f"listen: {state.get('phase', 'running')} {state.get('stem')} for {age:.0f}s{progress} "
          f"(quant {state.get('quant')}, log {state.get('log')})")
    return 0


def cmd_prefetch(args) -> int:
    """Download a build, SNAC and the tokenizer files into the Hugging Face cache."""
    from huggingface_hub import snapshot_download

    quants = args.quant or [listen_config.load().quant]
    jobs = [(listen_config.MODEL_REPO.format(quant=q), None) for q in quants]
    jobs += [(listen_config.SNAC_REPO, None), (listen_config.TOKENIZER_REPO, list(listen_config.TOKENIZER_FILES))]
    for repo, patterns in jobs:
        started = time.perf_counter()
        path = snapshot_download(repo, allow_patterns=patterns)
        print(f"listen: {repo}{' (tokenizer only)' if patterns else ''} ready in {time.perf_counter() - started:.0f}s: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="synthesize and play a script in the background")
    run.add_argument("script")
    run.add_argument("--foreground", action="store_true", help="do not detach; log to stdout too")
    run.add_argument("--no-play", action="store_true", help="write the WAV but do not play it")
    run.add_argument("--target-words", type=int, help=f"refuse (exit {LENGTH_EXIT}) unless the script is within "
                     f"{WORDS_LOW:.0%}-{WORDS_HIGH:.0%} of this many words")
    run.set_defaults(func=cmd_run)
    hidden = sub.add_parser("_worker")
    hidden.add_argument("script")
    hidden.add_argument("stem")
    hidden.add_argument("--no-play", action="store_true")
    hidden.set_defaults(func=cmd_worker)
    sub.add_parser("stop", help="stop the active run").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="show the active run").set_defaults(func=cmd_status)
    prefetch = sub.add_parser("prefetch", help="download model files (default: the configured quant)")
    prefetch.add_argument("--quant", action="append", choices=listen_config.QUANTS)
    prefetch.set_defaults(func=cmd_prefetch)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ListenError, listen_config.ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        if args.command == "run":
            notify(str(exc).removeprefix("listen: ").removeprefix("listen config: "), title="Listen failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
