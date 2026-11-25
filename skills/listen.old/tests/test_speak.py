"""Tests for scripts/speak.py that need no model (tasks 3.2, 3.4, 3.6)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import speak  # noqa: E402


def texts(text: str, **kwargs) -> list[str]:
    return [c.text for c in speak.chunk_text(text, **kwargs)]


# --------------------------------------------------------------------------- 3.2 chunking


def test_abbreviations_do_not_end_sentences():
    assert speak.split_sentences("Dr. Smith uses tools, e.g. a hammer. Then he leaves.") == [
        "Dr. Smith uses tools, e.g. a hammer.", "Then he leaves."]
    assert speak.split_sentences("We met J. R. Tolkien. He waved.") == ["We met J. R. Tolkien.", "He waved."]


def test_decimals_and_versions_stay_whole():
    assert speak.split_sentences("Version 2.4.1 shipped. It took 3.2 seconds, not 4.5!") == [
        "Version 2.4.1 shipped.", "It took 3.2 seconds, not 4.5!"]


def test_question_and_quotes():
    assert speak.split_sentences('Why? "Because it works." Fine.') == ["Why?", '"Because it works."', "Fine."]


def test_groups_at_most_three_sentences_and_300_chars():
    text = " ".join(f"Sentence number {i} is short." for i in range(10))
    chunks = texts(text)
    assert all(len(c) <= 300 for c in chunks)
    assert all(len(speak.split_sentences(c)) <= 3 for c in chunks)
    assert " ".join(chunks) == text


def test_long_sentence_is_split_at_clauses():
    sentence = ("Because the cache lives in memory on each node, a customer who updates their address, "
                "and then immediately downloads an invoice from the billing page, might for a short window, "
                "one that can last up to a quarter of an hour on a busy day, see the old address on it, "
                "and the team decided this was acceptable for now while they design invalidation.")
    assert len(sentence) > 300
    chunks = texts(sentence)
    assert len(chunks) >= 2 and all(len(c) <= 300 for c in chunks)
    assert all(c.rstrip().endswith((",", ".")) for c in chunks)
    assert " ".join(chunks) == sentence


def test_very_long_word_run_is_still_capped():
    chunks = texts("word " * 200)
    assert all(len(c) <= 300 for c in chunks)


def test_emotion_tags_kept_intact():
    chunks = texts("It is four times faster. <chuckle> Nobody expected that. <sigh> It is never free.")
    joined = " ".join(chunks)
    assert "<chuckle>" in joined and "<sigh>" in joined
    for chunk in chunks:
        assert chunk.count("<") == chunk.count(">")


def test_lone_short_sentence_pulls_a_neighbour():
    text = ("So here's the short version of what changed today. The team shipped version 2.4.1 of the "
            "billing service. Invoice generation dropped from about 3.2 seconds to 850 milliseconds. "
            "<chuckle> Nobody expected that.")
    chunks = texts(text)
    assert len(chunks) == 2
    assert chunks[1] == "Invoice generation dropped from about 3.2 seconds to 850 milliseconds. <chuckle> Nobody expected that."


def test_lone_short_sentence_joins_the_next_chunk_when_it_cannot_pull():
    long_tail = "And then " + "a very long clause, " * 13 + "the end."
    chunks = texts("Short one. " + long_tail)
    assert chunks[0].startswith("Short one. And then")
    assert all(len(c) <= 300 for c in chunks)


def test_rebalance_keeps_limits():
    groups = [["A" * 100, "B" * 100, "C" * 90], ["short."]]
    assert speak.rebalance(groups, 300, 3) == [["A" * 100, "B" * 100], ["C" * 90, "short."]]
    groups = [["A" * 150, "B" * 145], ["short."]]  # pulling B would exceed 300 chars? no: 145+7 fits
    assert speak.rebalance(groups, 300, 3) == [["A" * 150], ["B" * 145, "short."]]
    groups = [["tiny.", "B" * 100], ["short."]]  # would leave "tiny." alone: no pull, nothing after
    assert speak.rebalance(groups, 300, 3) == [["tiny.", "B" * 100], ["short."]]


def test_tag_alone_is_folded_into_next_chunk():
    chunks = texts("<laugh>\n\nThat was close.")
    assert chunks == ["<laugh> That was close."]


def test_tag_never_split_by_long_cut():
    text = ("a" * 10 + " ") * 26 + "<chuckle> " + "end of it."
    for chunk in texts(text):
        assert ("<" in chunk) == (">" in chunk)


def test_unknown_tags_and_markdown_removed():
    chunks = texts("# Title\n\n**Bold** and `code` and <b>html</b>. - item one\n\n```py\nprint(1)\n```\nDone.")
    joined = " ".join(chunks)
    for junk in ("#", "*", "`", "<b>", "</b>", "print(1)"):
        assert junk not in joined, junk
    assert "Bold and code and html." in joined and "Done." in joined


def test_paragraphs_get_longer_pauses():
    chunks = speak.chunk_text("One. Two.\n\nThree.")
    assert [c.pause_after for c in chunks] == [speak.PAUSE_PARAGRAPH, speak.PAUSE_PARAGRAPH]
    chunks = speak.chunk_text("One. Two. Three. Four.")
    assert chunks[0].pause_after == speak.PAUSE_SENTENCE


def test_max_tokens_scales_with_length():
    short = speak.Chunk("Hi there.")
    long = speak.Chunk("This is a considerably longer chunk of text, " * 5)
    assert short.max_tokens() >= speak.MIN_MAX_TOKENS
    assert long.max_tokens() > short.max_tokens()
    assert long.max_tokens() >= speak.TOKENS_PER_SECOND * long.expected_seconds()


def test_trim_silence_cuts_edges_only():
    np = pytest.importorskip("numpy")
    rate = speak.SAMPLE_RATE
    tone = 0.3 * np.sin(np.linspace(0, 2 * np.pi * 220, rate)).astype(np.float32)
    audio = np.concatenate([np.zeros(2 * rate, np.float32), tone, np.zeros(rate // 2, np.float32), tone,
                            np.zeros(2 * rate, np.float32)])
    trimmed = speak.trim_silence(audio)
    expected = 2.5 * rate + 2 * speak.EDGE_KEEP * rate
    assert abs(len(trimmed) - expected) < 0.05 * rate  # inner pause kept, edges cut


# --------------------------------------------------------------------------- 3.4 / 3.6 state and outputs


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CC_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VOICE_CC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VOICE_CC_NOTIFY", "0")
    (tmp_path / "out").mkdir()
    return tmp_path


def test_stop_with_nothing_active(dirs, capsys):
    assert speak.main(["stop"]) == 0
    assert capsys.readouterr().out.strip() == "listen: nothing to stop"


def test_stop_with_stale_state(dirs, capsys):
    speak.write_state({"pid": 999999, "stem": "20260101-000000-x"})
    assert speak.main(["stop"]) == 0
    assert "nothing to stop" in capsys.readouterr().out
    assert not speak.state_file().exists()


def test_status_idle(dirs, capsys):
    assert speak.main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "listen: idle"


def test_prune_keeps_newest_sets(dirs):
    out = dirs / "out"
    stems = ["20261101-090000-aaaaaaaa", "20261101-100000-bbbbbbbb", "20261101-110000-manual"]
    for stem in stems:
        for suffix in (".txt", ".wav", ".log", ".digest.md"):
            (out / f"{stem}{suffix}").write_text("x")
    (out / "notes.txt").write_text("not ours")
    removed = speak.prune(keep=2)
    assert len(removed) == 4
    left = sorted(p.name for p in out.iterdir())
    assert not any(n.startswith(stems[0]) for n in left)
    assert sum(n.startswith(stems[1]) for n in left) == 4 and sum(n.startswith(stems[2]) for n in left) == 4
    assert "notes.txt" in left


def test_run_paths_reuses_saved_script_and_copies_outside_ones(dirs):
    out = dirs / "out"
    saved = out / "20261101-090000-aaaaaaaa.txt"
    saved.write_text("Hello.")
    assert speak.run_paths(saved) == ("20261101-090000-aaaaaaaa", saved.resolve())
    outside = dirs / "draft.txt"
    outside.write_text("Hi.")
    stem, copied = speak.run_paths(outside)
    assert stem.endswith("-manual") and copied.parent == out and copied.read_text() == "Hi."


def test_run_rejects_missing_script(dirs, capsys):
    assert speak.main(["run", str(dirs / "nope.txt")]) == 1
    assert "missing or empty" in capsys.readouterr().err


def test_run_names_missing_model_build(dirs, monkeypatch, capsys):
    pytest.importorskip("huggingface_hub")
    monkeypatch.setenv("HF_HUB_CACHE", str(dirs / "empty-cache"))
    script = dirs / "s.txt"
    script.write_text("Hello there.")
    monkeypatch.setenv("VOICE_CC_QUANT", "6bit")
    assert speak.main(["run", str(script)]) == 1
    assert "mlx-community/orpheus-3b-0.1-ft-6bit is not installed" in capsys.readouterr().err


def test_run_length_gate(dirs, capsys):
    script = dirs / "s.txt"
    script.write_text(" ".join(["word"] * 251) + ".")
    assert speak.main(["run", str(script), "--target-words", "200"]) == speak.LENGTH_EXIT
    out = capsys.readouterr().out
    assert "has 251 words but must have 170 to 230" in out
    assert speak.read_state() is None  # nothing was started
