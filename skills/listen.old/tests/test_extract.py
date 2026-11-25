"""Tests for scripts/extract.py (tasks 2.2–2.6)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import extract  # noqa: E402

FIXTURES = HERE / "fixtures"
MIXED_EXCLUDED = [
    "SECRET-THINKING", "RAW-TOOL-OUTPUT", "SIDECHAIN-TEXT", "META-SKILL-BODY", "REJECTED-EDIT-TEXT",
    "LISTEN-REACTION", "IN-PROGRESS", "API Error", "Set model to Opus", "/listen", "[listen]",
]


def turns_of(name: str) -> list[extract.Turn]:
    return extract.segment(extract.read_records(FIXTURES / name))


def digest(name: str, scope: str = "last", budget: int = 60000, words: int = 300) -> tuple[str, dict]:
    return extract.build_digest(turns_of(name), extract.parse_scope(scope), budget, words, "test")


# --------------------------------------------------------------------------- 2.2 lookup


def test_find_transcript_found(tmp_path):
    target = tmp_path / "-Users-dev-demo" / "abc12345-0000.jsonl"
    target.parent.mkdir()
    target.write_text("{}\n")
    assert extract.find_transcript("abc12345-0000", tmp_path) == target


def test_find_transcript_not_found(tmp_path):
    (tmp_path / "-Users-dev-demo").mkdir()
    with pytest.raises(extract.ExtractError) as err:
        extract.find_transcript("abc12345-0000", tmp_path)
    message = str(err.value)
    assert "abc12345-0000" in message and "\n" not in message


def test_find_transcript_same_id_in_two_projects_picks_newest(tmp_path):
    old = tmp_path / "-a" / "abc12345-0000.jsonl"
    new = tmp_path / "-b" / "abc12345-0000.jsonl"
    for path in (old, new):
        path.parent.mkdir()
        path.write_text("{}\n")
    past = time.time() - 3600
    os.utime(old, (past, past))
    assert extract.find_transcript("abc12345-0000", tmp_path) == new


def test_find_transcript_rejects_path_like_ids(tmp_path):
    with pytest.raises(extract.ExtractError):
        extract.find_transcript("../../etc/passwd", tmp_path)


def test_cli_reports_missing_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_CC_CONFIG", str(tmp_path / "none.toml"))
    assert extract.main(["--session", "deadbeef-0000", "--stdout"]) == 1
    assert "no transcript for session deadbeef-0000" in capsys.readouterr().err


# --------------------------------------------------------------------------- 2.3 segmentation + exclusions


def test_mixed_turn_count_and_order():
    turns = turns_of("mixed.jsonl")
    prompts = [t.prompt for t in turns]
    assert prompts == [
        "What does the retry helper in the HTTP client do?",
        "Find every caller of the retry helper and check the timeouts.",
        "/fix-timeouts lower to 10s",
        "Now update the changelog.",
        "Summarise what we changed today in one paragraph, please.",
    ]


def test_mixed_excluded_content_is_absent():
    text, _ = digest("mixed.jsonl", "all")
    body = text.split("\n\n", 1)[1]  # the header names the tool itself
    for marker in MIXED_EXCLUDED:
        assert marker not in body, marker


def test_unrelated_notification_reply_stays_with_its_turn():
    turns = turns_of("mixed.jsonl")
    changelog = turns[3]
    assert any("full test suite passed" in i.text for i in changelog.items)


def test_repeated_listen_covers_the_same_turn():
    records = extract.read_records(FIXTURES / "mixed.jsonl")
    first_listen = next(i for i, r in enumerate(records)
                        if r.get("type") == "user" and extract.is_prompt(r) and extract.is_listen_prompt(r))
    once = records[: first_listen + 1]
    twice = records[: first_listen + 3] + [records[first_listen]]  # notification, reaction, /listen again
    for recs in (once, twice):
        text, meta = extract.build_digest(extract.segment(recs), 1, 60000, 300)
        assert "/fix-timeouts lower to 10s" in text
        assert "LISTEN-REACTION" not in text
        assert meta["turns_included"] == 1


def test_compact_summary_is_not_a_turn():
    text, meta = digest("compacted.jsonl", "all")
    assert "COMPACT-SUMMARY-TEXT" not in text
    assert meta["turns_available"] == 5


# --------------------------------------------------------------------------- 2.4 kept content


def test_write_of_openspec_artifact_is_in_digest():
    text, meta = digest("propose.jsonl", "last")
    assert meta["turns_available"] == 4
    assert "[wrote file: openspec/changes/add-listen-skill/proposal.md]" in text
    assert "## Why" in text
    assert "Claude Code produces long, dense output" in text  # the proposal body itself
    assert "## Decisions" in text  # design.md body too


def test_edit_path_and_new_text_and_markers():
    text, _ = digest("mixed.jsonl", "all")
    assert "[edited file (new text): src/client.py]" in text
    assert "TIMEOUT = 10  # matches the backoff budget" in text
    assert "[Edit of src/other.py failed or was rejected]" in text
    assert "[ran Bash: Run retry tests]" in text
    assert "[ran Grep: with_retry\\(]" in text
    assert "[ran Agent: Audit timeout values]" in text
    assert "# Changelog" in text


def test_prompt_and_reply_text_kept():
    text, _ = digest("mixed.jsonl", "all")
    assert "What does the retry helper in the HTTP client do?" in text
    assert "It retries idempotent requests up to three times" in text


# --------------------------------------------------------------------------- 2.5 scopes


@pytest.mark.parametrize("value", ["0", "-1", "abc", "2.5", "stop", "all of it"])
def test_invalid_scope(value):
    with pytest.raises(extract.ExtractError) as err:
        extract.parse_scope(value)
    assert str(err.value) == extract.USAGE


@pytest.mark.parametrize("value, expected", [(None, 1), ("", 1), ("last", 1), ("LAST", 1), ("3", 3), ("all", None)])
def test_valid_scope(value, expected):
    assert extract.parse_scope(value) == expected


def test_scope_last():
    text, meta = digest("mixed.jsonl", "last")
    assert meta["turns_included"] == 1 and meta["scope"] == "last turn"
    assert "=== Turn 5 of 5 ===" in text and "=== Turn 4 of 5 ===" not in text


def test_scope_n():
    text, meta = digest("mixed.jsonl", "3")
    assert meta["turns_included"] == 3 and meta["scope"] == "last 3 turns"
    assert "=== Turn 3 of 5 ===" in text and "=== Turn 2 of 5 ===" not in text
    assert not meta["notes"]


def test_scope_n_more_than_available():
    text, meta = digest("mixed.jsonl", "50")
    assert meta["turns_included"] == 5
    assert "note: only 5 turns available (requested 50)" in text


def test_scope_all_on_compacted_session_includes_pre_compaction_turns():
    text, meta = digest("compacted.jsonl", "all")
    assert meta["turns_included"] == 5 and meta["scope"] == "whole session (5 turns)"
    assert "billing database" in text and "dark mode" in text


def test_no_turns_is_a_clear_error():
    records = extract.read_records(FIXTURES / "compacted.jsonl")
    with pytest.raises(extract.ExtractError, match="no completed turns"):
        extract.build_digest(extract.segment(records[:0] + [records[-1]]), 1, 60000, 300)


def test_cli_writes_run_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICE_CC_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VOICE_CC_CONFIG", str(tmp_path / "none.toml"))
    code = extract.main(["--session", "00000000-1111", "--scope", "all", "--transcript", str(FIXTURES / "mixed.jsonl")])
    assert code == 0
    out = capsys.readouterr().out
    meta = json.loads(out.splitlines()[0])
    assert "----- style guide -----" in out and "## Written for the ear" in out
    assert Path(meta["digest_path"]).is_file()
    assert meta["script_path"].endswith("-00000000.txt") and meta["quant"]
    assert meta["turns_included"] == 5


def test_cli_usage_error_exit_code(capsys):
    assert extract.main(["--session", "00000000-1111", "--scope", "banana"]) == 2
    assert capsys.readouterr().err.strip() == extract.USAGE


# --------------------------------------------------------------------------- 2.6 size budget


def big_turns(count: int, size: int, prompt_size: int = 0) -> list[extract.Turn]:
    turns = []
    for n in range(count):
        body = "\n".join(f"line {n}-{i} " + "x" * 60 for i in range(size // 70))
        turns.append(extract.Turn(prompt=f"prompt {n} " + "p" * prompt_size, items=[
            extract.Item("text", f"reply {n} first"),
            extract.Item("write", body, f"docs/file{n}.md"),
            extract.Item("text", f"reply {n} final"),
        ]))
    return turns


def test_oversized_digest_fits_and_keeps_latest_turn():
    turns = big_turns(120, 6000)  # file previews alone are not enough at this size
    budget = 30000
    text, meta = extract.build_digest(turns, None, budget, 300)
    assert len(text) <= budget
    assert meta["condensed"] is True and "condensed: yes" in text
    assert "note: earlier parts were condensed to fit the size budget" in text
    latest = extract.render_turn(turns[-1], 120, 120)
    assert latest in text  # newest turn intact, file content included
    assert meta["turns_omitted"] == 0  # tier 2 (prompt + final reply) was enough
    assert "[condensed: earlier steps of this turn left out]" in text and "reply 0 final" in text
    assert "reply 0 first" not in text


def test_oldest_turns_dropped_when_condensing_is_not_enough():
    turns = big_turns(40, 6000, prompt_size=2000)
    text, meta = extract.build_digest(turns, None, 30000, 300)
    assert len(text) <= 30000
    assert meta["turns_omitted"] > 0 and f"[{meta['turns_omitted']} earlier turns omitted" in text
    assert "prompt 0 " not in text and extract.render_turn(turns[-1], 40, 40) in text
    assert meta["turns_included"] == 40 - meta["turns_omitted"]


def test_mild_overage_only_previews_older_files():
    turns = big_turns(4, 6000)
    text, meta = extract.build_digest(turns, None, 17000, 300)
    assert len(text) <= 17000
    assert meta["condensed"] and meta["turns_omitted"] == 0
    assert "more lines condensed" in text
    assert "reply 0 first" in text  # tier 1 keeps the turn structure
    assert extract.render_turn(turns[-1], 4, 4) in text


def test_within_budget_is_not_condensed():
    text, meta = digest("mixed.jsonl", "all")
    assert meta["condensed"] is False and "condensed: no" in text and "note:" not in text


def test_target_words_scale_down_for_small_content():
    _, small = digest("mixed.jsonl", "last", words=300)
    _, large = digest("propose.jsonl", "last", words=300)
    assert small["target_words"] < 300
    assert large["target_words"] == 300


def test_headless_sdk_prompts_count_as_turns():
    """claude -p records prompts with no origin and turnOrigin "sdk"."""
    records = [
        {"type": "user", "uuid": "u1", "turnOrigin": "sdk", "promptSource": "sdk", "entrypoint": "sdk-cli",
         "message": {"role": "user", "content": "Write the proposal."}},
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/tmp/p.md", "content": "# Rate limits"}}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "w1", "content": "ok"}]}},
        {"type": "user", "uuid": "u3", "isMeta": True, "message": {"role": "user", "content": "<local-command-caveat>x</local-command-caveat>"}},
        {"type": "user", "uuid": "u4", "entrypoint": "sdk-cli", "message": {"role": "user", "content": "/listen"}},
    ]
    turns = extract.segment(records)
    assert [t.prompt for t in turns] == ["Write the proposal."]
    assert turns[0].items[0].kind == "write" and "# Rate limits" in turns[0].items[0].text
