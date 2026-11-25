# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Turn a Claude Code session transcript into a plain-text digest for /listen.

Usage:
  extract.py --session ID [--scope last|N|all]          # write digest for a new run, print JSON
  extract.py --session ID --scope 3 --stdout            # print the digest instead

The transcript format is internal to Claude Code (tested with 2.1.331+). Anything
unexpected fails with a one-line reason rather than an empty digest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_config  # noqa: E402

USAGE = "Usage: /listen [last | N | all | stop]  (N = a positive number of recent turns)"

LISTEN_COMMAND = re.compile(r"<command-name>\s*/listen\s*</command-name>|^\s*/listen(\s|$)")
# Marker the /listen subagent puts at the start of its one-line result, so the
# notification it produces can be recognised and left out of later digests.
LISTEN_MARK = "[listen]"
LISTEN_NOTIFICATION = re.compile(r"\[listen\]|<summary>[^<]*\blisten\b[^<]*</summary>", re.IGNORECASE)
NOT_A_PROMPT = re.compile(
    r"^\s*(<local-command-stdout>|<local-command-caveat>|<task-notification>|<agent-message"
    r"|\[Request interrupted by user)"
)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
COMMAND_PARTS = re.compile(r"<command-(name|args)>(.*?)</command-\1>", re.DOTALL)
COMMAND_TAGS = re.compile(r"</?command-(message|name|args)>")

PREVIEW_LINES = 5
HEADER_RESERVE = 600  # characters kept free for the digest header
WRAP_AT = 500  # keep lines short enough for the Read tool
MIN_TARGET_WORDS = 90
WORDS_PER_MINUTE = 140  # Orpheus tara, measured in bench/RESULTS.md


class ExtractError(Exception):
    """Fatal problem; the message is one line, fit for the user."""


# --------------------------------------------------------------------------- lookup


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def find_transcript(session_id: str, root: Path | None = None) -> Path:
    """Return ~/.claude/projects/*/<session_id>.jsonl (newest if several match)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", session_id or ""):
        raise ExtractError(f"listen: invalid session id {session_id!r}")
    root = projects_dir() if root is None else root
    matches = [p for p in root.glob(f"*/{session_id}.jsonl") if p.is_file()]
    if not matches:
        raise ExtractError(f"listen: no transcript for session {session_id} under {root}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def read_records(path: Path) -> list[dict]:
    records, seen, bad = [], set(), 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad += 1  # a partially written last line is normal while the session runs
                continue
            if not isinstance(record, dict):
                continue
            uuid = record.get("uuid")
            if uuid:
                if uuid in seen:
                    continue
                seen.add(uuid)
            records.append(record)
    if not records:
        raise ExtractError(f"listen: transcript {path} has no readable records")
    return records


# --------------------------------------------------------------------------- record classification


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _blocks(record: dict) -> list:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def is_excluded(record: dict) -> bool:
    return bool(
        record.get("isSidechain")
        or record.get("isMeta")
        or record.get("isCompactSummary")
        or record.get("isVisibleInTranscriptOnly")
    )


def is_prompt(record: dict) -> bool:
    """A real user prompt: typed (or accepted) by the human, not tool output or harness text."""
    if record.get("type") != "user" or is_excluded(record):
        return False
    blocks = _blocks(record)
    if not blocks or any(b.get("type") == "tool_result" for b in blocks):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    if record.get("turnOrigin") not in (None, "human", "sdk"):  # sdk: claude -p / Agent SDK prompts
        return False
    text = _text_of(record["message"]["content"])
    return bool(text.strip()) and not NOT_A_PROMPT.match(text)


def is_notification(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isSidechain"):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
        return True
    return record.get("turnOrigin") == "task_notification" or _text_of(
        (record.get("message") or {}).get("content")
    ).lstrip().startswith("<task-notification>")


def is_listen_prompt(record: dict) -> bool:
    return bool(LISTEN_COMMAND.search(_text_of(record["message"]["content"])))


def is_listen_notification(record: dict) -> bool:
    return bool(LISTEN_NOTIFICATION.search(_text_of((record.get("message") or {}).get("content"))))


def clean_prompt(text: str) -> str:
    text = SYSTEM_REMINDER.sub("", text)
    if "<command-name>" in text:
        parts = dict((k, v.strip()) for k, v in COMMAND_PARTS.findall(text))
        rest = re.sub(r"<command-(message|name|args)>.*?</command-\1>", "", text, flags=re.DOTALL)
        rest = COMMAND_TAGS.sub("", rest).strip()
        text = " ".join(x for x in (parts.get("name", ""), parts.get("args", ""), rest) if x)
    return text.strip()


# --------------------------------------------------------------------------- turns


@dataclass
class Item:
    kind: str  # text | write | edit | tool
    text: str = ""
    path: str = ""


@dataclass
class Turn:
    prompt: str
    cwd: str = ""
    items: list[Item] = field(default_factory=list)
    is_listen: bool = False


def _display_path(path: str, cwd: str) -> str:
    if not path:
        return "(unknown file)"
    try:
        p = Path(path)
        if cwd and p.is_absolute() and p.is_relative_to(cwd):
            return str(p.relative_to(cwd))
        home = Path.home()
        if p.is_absolute() and p.is_relative_to(home):
            return "~/" + str(p.relative_to(home))
    except (ValueError, OSError):
        pass
    return path


def _tool_marker(name: str, data: dict) -> str:
    detail = ""
    if name == "Bash":
        detail = data.get("description") or data.get("command", "")
    else:
        for key in ("description", "file_path", "notebook_path", "pattern", "query", "url", "skill", "prompt", "command"):
            if isinstance(data.get(key), str) and data[key].strip():
                detail = data[key]
                break
    detail = " ".join(str(detail).split())
    if len(detail) > 120:
        detail = detail[:117] + "..."
    return f"[ran {name}: {detail}]" if detail else f"[ran {name}]"


def _items_for_tool(block: dict, cwd: str, failed: bool) -> list[Item]:
    name = str(block.get("name", "tool"))
    data = block.get("input") if isinstance(block.get("input"), dict) else {}
    path = _display_path(str(data.get("file_path") or data.get("notebook_path") or ""), cwd)
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and failed:
        return [Item("tool", f"[{name} of {path} failed or was rejected]")]
    if name == "Write":
        return [Item("write", str(data.get("content", "")), path)]
    if name == "Edit":
        return [Item("edit", str(data.get("new_string", "")), path)]
    if name == "MultiEdit":
        edits = data.get("edits") if isinstance(data.get("edits"), list) else []
        return [Item("edit", "\n...\n".join(str(e.get("new_string", "")) for e in edits if isinstance(e, dict)), path)]
    if name == "NotebookEdit":
        return [Item("edit", str(data.get("new_source", "")), path)]
    return [Item("tool", _tool_marker(name, data))]


def segment(records: list[dict]) -> list[Turn]:
    """Split records into turns, applying the exclusion rules (design D2)."""
    failed_tools = set()
    for record in records:
        if record.get("type") == "user":
            for b in _blocks(record):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    failed_tools.add(b.get("tool_use_id"))

    turns: list[Turn] = []
    current: Turn | None = None
    skipping = False  # inside a /listen notification and Claude's reaction to it
    last_listen = -1
    for record in records:
        kind = record.get("type")
        if kind == "user":
            if is_prompt(record):
                text = _text_of(record["message"]["content"])
                current = Turn(prompt=clean_prompt(text), cwd=str(record.get("cwd") or ""))
                current.is_listen = is_listen_prompt(record)
                turns.append(current)
                if current.is_listen:
                    last_listen = len(turns) - 1
                skipping = False
            elif is_notification(record):
                skipping = is_listen_notification(record)
            continue
        if kind != "assistant" or record.get("isSidechain") or record.get("isApiErrorMessage"):
            continue
        if skipping or current is None or current.is_listen:
            continue
        for block in _blocks(record):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                current.items.append(Item("text", block["text"].strip()))
            elif btype == "tool_use":
                current.items.extend(_items_for_tool(block, current.cwd, block.get("id") in failed_tools))
            # thinking, redacted_thinking and anything else are left out on purpose

    # The newest /listen request is the one running now: anything after it is in progress.
    if last_listen >= 0:
        turns = turns[:last_listen]
    return [t for t in turns if not t.is_listen and t.items]


# --------------------------------------------------------------------------- scope + budget


def parse_scope(value: str | None) -> int | None:
    """Return the number of turns wanted, or None for all. Raises ExtractError(USAGE)."""
    value = (value or "last").strip().lower()
    if value in ("", "last"):
        return 1
    if value == "all":
        return None
    if re.fullmatch(r"\d{1,6}", value) and int(value) > 0:
        return int(value)
    raise ExtractError(USAGE)


def _wrap(text: str) -> str:
    out = []
    for line in text.splitlines():
        while len(line) > WRAP_AT:
            cut = line.rfind(" ", 0, WRAP_AT)
            cut = cut if cut > WRAP_AT // 2 else WRAP_AT
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return "\n".join(out)


def render_turn(turn: Turn, number: int, total: int, tier: int = 0) -> str:
    """tier 0 = full, 1 = file contents previewed, 2 = prompt + final reply only."""
    lines = [f"=== Turn {number} of {total} ===", "User:", turn.prompt or "(empty prompt)", "", "Claude:"]
    items = turn.items
    if tier >= 2:
        texts = [i for i in items if i.kind == "text"]
        items = texts[-1:] if texts else [Item("tool", f"[{len(turn.items)} tool calls, no reply text]")]
        lines.append("[condensed: earlier steps of this turn left out]")
    for item in items:
        if item.kind == "text":
            lines += [item.text, ""]
        elif item.kind == "tool":
            lines.append(item.text)
        else:
            verb = "wrote file" if item.kind == "write" else "edited file (new text)"
            body = item.text.rstrip("\n")
            if tier >= 1:
                body_lines = body.splitlines()
                if len(body_lines) > PREVIEW_LINES:
                    body = "\n".join(body_lines[:PREVIEW_LINES]) + f"\n[... {len(body_lines) - PREVIEW_LINES} more lines condensed]"
            lines += [f"[{verb}: {item.path}]", "<<<", body, ">>>", ""]
    return _wrap("\n".join(lines).rstrip() + "\n")


def scope_label(requested: int | None, included: int) -> str:
    if requested is None:
        return f"whole session ({included} turn{'s' * (included != 1)})"
    if requested == 1 or included == 1:
        return "last turn"
    return f"last {included} turns"


def build_digest(turns: list[Turn], requested: int | None, budget: int, words: int, session_id: str = "") -> tuple[str, dict]:
    if not turns:
        raise ExtractError("listen: no completed turns to explain yet in this session")
    available = len(turns)
    selected = turns if requested is None else turns[-requested:]
    total = len(selected)
    tiers = [0] * total
    dropped = 0  # oldest turns removed entirely

    def body() -> str:
        parts = []
        if dropped:
            parts.append(f"[{dropped} earlier turn{'s' * (dropped != 1)} omitted to fit the size budget]\n")
        offset = available - total
        for idx in range(dropped, total):
            parts.append(render_turn(selected[idx], offset + idx + 1, available, tiers[idx]))
        return "\n".join(parts)

    budget = max(1000, budget - HEADER_RESERVE)  # the budget covers the whole digest
    text = body()
    for tier in (1, 2):  # condense older turns oldest-first; the newest turn is never touched
        for idx in range(total - 1):
            if len(text) <= budget:
                break
            tiers[idx] = tier
            text = body()
    while len(text) > budget and dropped < total - 1:
        dropped += 1
        text = body()

    condensed = dropped > 0 or any(tiers)
    included = total - dropped
    content_words = len(text.split())
    target = min(words, max(MIN_TARGET_WORDS, round(content_words * 0.5 / 10) * 10))
    meta = {
        "session": session_id,
        "scope": scope_label(requested, total),
        "turns_requested": "all" if requested is None else requested,
        "turns_available": available,
        "turns_included": included,
        "turns_omitted": dropped,
        "condensed": condensed,
        "target_words": target,
        "estimated_minutes": round(target / WORDS_PER_MINUTE, 1),
    }
    notes = []
    if requested is not None and requested > available:
        notes.append(f"only {available} turn{'s' * (available != 1)} available (requested {requested})")
    if condensed:
        notes.append("earlier parts were condensed to fit the size budget")
    meta["notes"] = notes
    shown = {k: ("yes" if v is True else "no" if v is False else v) for k, v in meta.items() if k != "notes"}
    header = ["# /listen digest"] + [f"{k}: {v}" for k, v in shown.items()]
    header += [f"note: {n}" for n in notes]
    return "\n".join(header) + "\n\n" + text, meta


# --------------------------------------------------------------------------- CLI


def run_stem(session_id: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now:%Y%m%d-%H%M%S}-{session_id[:8]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="Claude Code session id")
    parser.add_argument("--scope", default="last", help="last | N | all (default: last)")
    parser.add_argument("--transcript", type=Path, help="read this .jsonl instead of looking it up")
    parser.add_argument("--max-chars", type=int, help="digest size budget (default: config digest_chars)")
    parser.add_argument("--stdout", action="store_true", help="print the digest instead of writing run files")
    args = parser.parse_args(argv)
    try:
        requested = parse_scope(args.scope)
        config = listen_config.load()
        path = args.transcript or find_transcript(args.session)
        turns = segment(read_records(path))
        text, meta = build_digest(turns, requested, args.max_chars or config.digest_chars, config.words, args.session)
        if args.stdout:
            sys.stdout.write(text)
            return 0
        out = listen_config.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        stem = run_stem(args.session)
        digest_path = out / f"{stem}.digest.md"
        digest_path.write_text(text, encoding="utf-8")
        meta.update(digest_path=str(digest_path), script_path=str(out / f"{stem}.txt"), quant=config.quant)
        print(json.dumps(meta))
        # The subagent cannot Read files under ~/.claude, so the style guide travels on stdout.
        guide = Path(__file__).resolve().parent.parent / "style-guide.md"
        if guide.is_file():
            print("\n----- style guide -----\n" + guide.read_text(encoding="utf-8").strip())
        return 0
    except (ExtractError, listen_config.ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if str(exc) == USAGE else 1


if __name__ == "__main__":
    sys.exit(main())
