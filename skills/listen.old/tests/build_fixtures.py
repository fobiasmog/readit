"""Rebuild the transcript fixtures under tests/fixtures/.

- propose.jsonl   A real /openspec-explore + /opsx:propose session from this repo,
                  sanitized: only user/assistant/system records, a whitelist of keys,
                  home paths and ids replaced, thinking text and tool output removed.
                  Needs the original transcript: --source PATH.
- mixed.jsonl     Record shapes copied from real transcripts (Claude Code 2.1.33x),
                  neutral content: plain and tool-heavy turns, Edit (one failed),
                  sidechain and meta records, a local command, an interrupt, an API
                  error, earlier /listen turns and their notifications, an unrelated
                  task notification, the in-progress /listen and a prompt queued after it.
- compacted.jsonl Same approach: turns about topic A, an auto-compaction boundary
                  plus summary record, then turns about topic B.

Run: python3 build_fixtures.py [--source ~/.claude/projects/<slug>/<id>.jsonl]
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent / "fixtures"
SESSION = "00000000-1111-2222-3333-444444444444"
CWD = "/Users/dev/projects/demo"
KEEP_KEYS = {
    "type", "subtype", "uuid", "parentUuid", "isMeta", "isSidechain", "isCompactSummary",
    "isVisibleInTranscriptOnly", "isApiErrorMessage", "origin", "turnOrigin", "promptSource",
    "cwd", "sessionId", "timestamp", "message", "content", "level", "version",
}

_ids = itertools.count(1)
_clock = itertools.count(0)


def uid() -> str:
    return f"00000000-0000-4000-8000-{next(_ids):012d}"


def ts() -> str:
    seconds = next(_clock) * 7
    return f"2026-11-20T10:{seconds // 60 % 60:02d}:{seconds % 60:02d}.000Z"


def base(kind: str, **extra) -> dict:
    record = {"type": kind, "uuid": uid(), "sessionId": SESSION, "cwd": CWD, "timestamp": ts(),
              "isSidechain": False, "version": "2.1.331"}
    record.update(extra)
    return record


def prompt(text: str, source: str = "typed") -> dict:
    return base("user", origin={"kind": "human"}, turnOrigin="human", promptSource=source,
                message={"role": "user", "content": text})


def command(name: str, args: str = "") -> dict:
    text = f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
    if args:
        text += f"\n<command-args>{args}</command-args>"
    return prompt(text, source=None)


def meta(text: str) -> dict:
    return base("user", isMeta=True, message={"role": "user", "content": [{"type": "text", "text": text}]})


def notification(summary: str, result: str = "", run: str = "a1b2c3") -> dict:
    body = (f"<task-notification>\n<task-id>{run}</task-id>\n<status>completed</status>\n"
            f"<summary>{summary}</summary>\n" + (f"<result>{result}</result>\n" if result else "") +
            "</task-notification>")
    return base("user", origin={"kind": "task-notification", "producer": "session-task"},
                turnOrigin="task_notification", promptSource="system",
                message={"role": "user", "content": body})


def assistant(*blocks: dict, sidechain: bool = False, **extra) -> dict:
    record = base("assistant", message={"role": "assistant", "model": "claude-opus-5-5",
                                         "content": list(blocks), "stop_reason": "end_turn"}, **extra)
    record["isSidechain"] = sidechain
    return record


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking(t: str) -> dict:
    return {"type": "thinking", "thinking": t, "signature": "sig"}


def tool(name: str, tool_id: str, **inputs) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inputs}


def result(tool_id: str, output: str, error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": output}
    if error:
        block["is_error"] = True
    return base("user", message={"role": "user", "content": [block]})


def system(subtype: str, content: str = "", **extra) -> dict:
    return base("system", subtype=subtype, content=content, isMeta=False, level="info", **extra)


def noise() -> list[dict]:
    """Non-message records that appear between turns in real transcripts."""
    return [{"type": "ai-title", "aiTitle": "Demo", "sessionId": SESSION},
            {"type": "file-history-snapshot", "messageId": uid(), "snapshot": {}},
            {"type": "attachment", "uuid": uid(), "attachment": {"type": "todo"}, "sessionId": SESSION}]


def mixed() -> list[dict]:
    r: list[dict] = []
    # Turn 1: plain question and answer.
    r += noise()
    r.append(prompt("What does the retry helper in the HTTP client do?"))
    r.append(assistant(thinking("SECRET-THINKING: the user wants a short answer.")))
    r.append(assistant(text("It retries idempotent requests up to three times with exponential backoff.")))
    r.append(system("turn_duration", durationMs=4000))
    # Turn 2: tool-heavy, with a sidechain subagent and its raw output.
    r.append(prompt("Find every caller of the retry helper and check the timeouts."))
    r.append(assistant(tool("Grep", "t1", pattern="with_retry\\(", path="src")))
    r.append(result("t1", "RAW-TOOL-OUTPUT src/a.py:10 src/b.py:22"))
    r.append(assistant(tool("Agent", "t2", description="Audit timeout values", prompt="Look at timeouts")))
    r.append(assistant(text("SIDECHAIN-TEXT: subagent working notes."), sidechain=True))
    r.append(result("t2", "RAW-TOOL-OUTPUT subagent report"))
    r.append(assistant(tool("Bash", "t3", command="pytest -q tests/test_retry.py", description="Run retry tests")))
    r.append(result("t3", "RAW-TOOL-OUTPUT 12 passed"))
    r.append(assistant(text("There are two callers. Both use a 30 second timeout, which is longer than the backoff budget.")))
    # Turn 3: an Edit (and a rejected Edit), plus a skill body injected as a meta record.
    r.append(command("fix-timeouts", "lower to 10s"))
    r.append(meta("Base directory for this skill: /Users/dev/.claude/skills/fix-timeouts\n\nMETA-SKILL-BODY"))
    r.append(assistant(tool("Edit", "t4", file_path=f"{CWD}/src/client.py",
                            old_string="TIMEOUT = 30", new_string="TIMEOUT = 10  # matches the backoff budget")))
    r.append(result("t4", "The file has been updated."))
    r.append(assistant(tool("Edit", "t5", file_path=f"{CWD}/src/other.py",
                            old_string="x", new_string="REJECTED-EDIT-TEXT")))
    r.append(result("t5", "The user rejected this edit.", error=True))
    r.append(assistant(text("I lowered the client timeout to ten seconds.")))
    # A local command (no reply from Claude) and an interrupted request.
    r.append(base("user", message={"role": "user", "content":
                  "<command-name>/model</command-name>\n<command-message>model</command-message>"}))
    r.append(base("user", message={"role": "user", "content":
                  "<local-command-stdout>Set model to Opus</local-command-stdout>"}))
    # Turn 4: /listen (earlier run), its notification, and Claude's reaction to it.
    r.append(command("listen"))
    r.append(notification('Skill "listen" completed', "[listen] last turn · ~1 min · 8bit · plays when ready"))
    r.append(assistant(text("LISTEN-REACTION: the audio summary is being generated.")))
    # Turn 5: a real turn, then an unrelated background task finishing.
    r.append(prompt("Now update the changelog."))
    r.append(assistant(tool("Write", "t6", file_path=f"{CWD}/CHANGELOG.md",
                            content="# Changelog\n\n## 1.4.0\n- Client timeout lowered to 10 seconds.\n")))
    r.append(result("t6", "File created successfully"))
    r.append(assistant(text("The changelog now has a 1.4.0 entry.")))
    r.append(notification('Background command "Run full test suite" completed (exit code 0)', run="bg1"))
    r.append(assistant(text("The full test suite passed in the background.")))
    r.append(base("user", message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}))
    r.append(assistant(text("API Error: overloaded"), isApiErrorMessage=True))
    # Turn 6: /listen 2 (earlier run) whose notification arrives after the next prompt.
    r.append(command("listen", "2"))
    # Turn 7: last real turn before the current /listen.
    r.append(prompt("Summarise what we changed today in one paragraph."))
    r.append(notification('Skill "listen" completed', "[listen] last 2 turns · ~2 min · 8bit · plays when ready"))
    r.append(assistant(text("LISTEN-REACTION-2: noted.")))
    r.append(prompt("Summarise what we changed today in one paragraph, please.", source="queued"))
    r.append(assistant(text("Today we lowered the HTTP client timeout to ten seconds and recorded it in the changelog.")))
    r.append(system("turn_duration", durationMs=3000))
    # The /listen running now, and a prompt the user queued while it runs (in progress).
    r.append(command("listen", "all"))
    r.append(prompt("IN-PROGRESS-PROMPT: start on the docs."))
    r.append(assistant(text("IN-PROGRESS-REPLY")))
    return r


def compacted() -> list[dict]:
    r: list[dict] = []
    topics_a = [
        ("Plan the migration of the billing database to Postgres 17.", "Billing migration plan: dual-write for a week, then cut over."),
        ("How do we roll back the billing migration if invoices drift?", "Keep the old billing database read-only for thirty days as the rollback path."),
        ("Write the billing migration runbook.", "The billing runbook is written."),
    ]
    for question, answer in topics_a:
        r.append(prompt(question))
        r.append(assistant(text(answer)))
    r.append(system("compact_boundary", "Conversation compacted",
                    compactMetadata={"trigger": "auto", "preTokens": 180000, "postTokens": 12000}))
    r.append(base("user", isCompactSummary=True, isVisibleInTranscriptOnly=True,
                  message={"role": "user", "content": "This session is being continued from a previous "
                           "conversation that ran out of context. COMPACT-SUMMARY-TEXT"}))
    for question, answer in [
        ("Add a dark mode toggle to the settings page.", "The dark mode toggle is in place."),
        ("Make the dark mode toggle remember the choice.", "The dark mode choice is now saved per user."),
    ]:
        r.append(prompt(question))
        r.append(assistant(text(answer)))
    r.append(command("listen", "all"))
    return r


def sanitize(record: dict, home: str) -> dict | None:
    if record.get("type") not in ("user", "assistant", "system"):
        return None
    clean = {k: v for k, v in record.items() if k in KEEP_KEYS}
    clean["sessionId"] = SESSION
    message = clean.get("message")
    if isinstance(message, dict):
        message = {k: v for k, v in message.items() if k in ("role", "content", "model", "stop_reason", "id")}
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for b in content:
                if b.get("type") == "thinking":
                    b = {"type": "thinking", "thinking": "(removed)", "signature": "sig"}
                elif b.get("type") == "tool_result":
                    b = {k: v for k, v in b.items() if k in ("type", "tool_use_id", "is_error")}
                    b["content"] = "(tool output removed)"
                elif b.get("type") == "tool_use":
                    b = {k: v for k, v in b.items() if k in ("type", "id", "name", "input")}
                blocks.append(b)
            message["content"] = blocks
        clean["message"] = message
    text = json.dumps(clean, ensure_ascii=False)
    text = text.replace(home, "/Users/dev").replace(home.replace("/", "-"), "-Users-dev")
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  lambda m: m.group(0) if m.group(0) == SESSION else "00000000-0000-4000-8000-" + m.group(0)[-12:], text)
    return json.loads(text)


def write(name: str, records: list[dict]) -> None:
    path = HERE / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    print(f"wrote {path} ({len(records)} records)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, help="real transcript to sanitize into propose.jsonl")
    args = parser.parse_args()
    HERE.mkdir(parents=True, exist_ok=True)
    write("mixed.jsonl", mixed())
    write("compacted.jsonl", compacted())
    if args.source:
        home = str(Path.home())
        records = [sanitize(json.loads(line), home) for line in args.source.open() if line.strip()]
        write("propose.jsonl", [r for r in records if r])


if __name__ == "__main__":
    main()
