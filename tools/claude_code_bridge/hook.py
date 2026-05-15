#!/usr/bin/env python3
"""Claude Code hook → buddy-daemon adapter.

Configured as a hook command in `.claude/settings.json`. Reads the Claude
Code hook payload from stdin and forwards it to the daemon over
`/tmp/claude-buddy.sock`.

For most events (PostToolUse, SessionStart, etc.) this is fire-and-forget.
For PreToolUse, the hook blocks waiting for the daemon to relay the device's
approve/deny decision back over the same socket connection.

If the daemon is unreachable (not running, device disconnected) the hook
exits silently with no stdout, causing Claude Code to fall through to its
normal terminal permission prompt.
"""
from __future__ import annotations

import json
import os
import socket
import sys

SOCK_PATH = os.environ.get("CLAUDE_BUDDY_SOCK", "/tmp/claude-buddy.sock")
PRE_TOOL_TIMEOUT = 120.0  # seconds to wait for device decision


def send(payload: dict, timeout: float = 2.0) -> None:
    """Fire-and-forget send to the daemon. Silent on failure."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        s.close()
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
        print(f"[buddy-hook] daemon unreachable: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[buddy-hook] unexpected error: {e}", file=sys.stderr)


def request_response(payload: dict, timeout: float = PRE_TOOL_TIMEOUT) -> dict | None:
    """Send request to daemon and block waiting for a response line."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        # Wait for response
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        if data.strip():
            return json.loads(data.strip())
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
        print(f"[buddy-hook] PreToolUse daemon unreachable: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[buddy-hook] PreToolUse error: {e}", file=sys.stderr)
    return None


def main() -> None:
    try:
        evt = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    name = evt.get("hook_event_name", "")
    sid = evt.get("session_id", "")

    if name == "PreToolUse":
        tool = evt.get("tool_name", "")
        tool_input = evt.get("tool_input", {})

        # Build a human-readable summary of what's being requested
        summary = tool
        hint = ""
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path", "")
            command = tool_input.get("command", "")
            if tool == "Bash" and command:
                summary = "Run shell command"
                hint = command[:96]
            elif tool == "Edit" and file_path:
                summary = f"Edit {file_path}"[:56]
                old = tool_input.get("old_string", "")
                new = tool_input.get("new_string", "")
                hint = f"-{old[:40]}\n+{new[:40]}" if old else new[:96]
            elif tool == "Write" and file_path:
                summary = f"Write {file_path}"[:56]
                hint = tool_input.get("content", "")[:96]
            elif tool == "Read" and file_path:
                summary = f"Read {file_path}"[:56]
            elif file_path:
                summary = f"{tool} {file_path}"[:56]
                hint = str(tool_input.get("command",
                           tool_input.get("content", "")))[:96]
            elif command:
                summary = f"{tool}: {command[:40]}"[:56]
                hint = command[:96]
            else:
                hint = str(next(iter(tool_input.values()), ""))[:96]

        resp = request_response({
            "event": "PreToolUse",
            "tool_use_id": evt.get("tool_use_id", ""),
            "tool": tool,
            "summary": summary,
            "hint": hint,
            "session_id": sid,
        })

        if resp and resp.get("decision") == "allow":
            output = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }}
            print(json.dumps(output))
        elif resp and resp.get("decision") == "deny":
            reason = resp.get("reason", "Denied from device")
            output = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}
            print(json.dumps(output))
        # else: abstain — output nothing, Claude Code prompts in terminal

    elif name == "SessionStart":
        send({"event": "SessionStart", "session_id": sid})
    elif name == "PostToolUse":
        send({"event": "PostToolUse",
              "tool": evt.get("tool_name", ""),
              "session_id": sid})
    elif name == "Stop":
        send({"event": "Stop",
              "session_id": sid,
              "transcript_path": evt.get("transcript_path", "")})
    elif name == "SessionEnd":
        send({"event": "SessionEnd", "session_id": sid})
    elif name == "Notification":
        send({"event": "Notification",
              "msg": (evt.get("message") or "attention")[:23]})

    sys.exit(0)


if __name__ == "__main__":
    main()
