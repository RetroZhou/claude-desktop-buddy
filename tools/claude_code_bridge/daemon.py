#!/usr/bin/env python3
"""Claude Code ↔ Hardware Buddy BLE bridge daemon.

Maintains a long-lived BLE connection to a Claude-* device and exposes a
Unix socket so short-lived hook scripts can push session state without
paying the BLE reconnect cost on every event.

Supports device-side permission decisions: when a PreToolUse hook fires,
the daemon pushes the prompt to the device over BLE. The user can approve
or deny from the M5StickC buttons, and the decision flows back to Claude
Code via the hook script.

Wire protocol on the BLE side: see ../REFERENCE.md.
Wire protocol on the Unix socket: newline-delimited JSON, see hook.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.stderr.write(
        "bleak not installed. Run: pip install -r requirements.txt\n"
    )
    sys.exit(1)


NUS_SVC = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # central writes here
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # central subscribes here

SOCK_PATH = os.environ.get("CLAUDE_BUDDY_SOCK", "/tmp/claude-buddy.sock")
DEVICE_NAME_PREFIX = "Claude"
SNAPSHOT_INTERVAL = 5.0          # idle keepalive cadence (device times out at 30s)
NOTIFY_FLASH_S = 6.0             # how long to keep `completed` true (buzz window)
NOTIFY_MSG_S = 8.0               # how long to surface notification text in `msg`
RECENT_LINES_MAX = 5
PROMPT_TIMEOUT = 120.0           # seconds to wait for device approval/deny

# Module-level BLE connection state
ble_connected = False


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class PendingPrompt:
    """A PreToolUse request waiting for device response."""

    def __init__(self, tool_use_id: str, tool: str, summary: str, hint: str) -> None:
        self.tool_use_id = tool_use_id
        self.tool = tool
        self.summary = summary
        self.hint = hint
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.created_at: float = time.time()


class State:
    """Shared mutable state between the BLE loop and the hook server."""

    def __init__(self) -> None:
        self.active_sessions: set[str] = set()
        self.recent_lines: list[str] = []
        self.tokens: int = 0
        self.tokens_today: int = 0
        self.tokens_today_date = datetime.now().date()
        self.transcript_seen: dict[str, int] = {}      # transcript_path → bytes consumed
        self.notify_flash_until: float = 0.0           # epoch s; sets `completed` true while in window
        self.notify_msg: str = ""                      # short text shown briefly in snapshot.msg
        self.notify_msg_until: float = 0.0             # epoch s; show notify_msg while now < this
        self.dirty = asyncio.Event()                   # set when snapshot needs rebuild
        self.pending_prompt: PendingPrompt | None = None  # device approval in progress

    def add_line(self, text: str) -> None:
        self.recent_lines.append(text[:90])
        del self.recent_lines[:-RECENT_LINES_MAX]

    def roll_today_if_needed(self) -> None:
        today = datetime.now().date()
        if today != self.tokens_today_date:
            self.tokens_today_date = today
            self.tokens_today = 0

    def snapshot(self) -> dict:
        self.roll_today_if_needed()
        total = len(self.active_sessions)
        running = total
        now = time.time()
        if now < self.notify_msg_until and self.notify_msg:
            msg = self.notify_msg
        else:
            msg = "running" if running else ("idle" if total else "no sessions")
        snap: dict = {
            "total": total,
            "running": running,
            "waiting": 1 if self.pending_prompt else 0,
            "msg": msg[:23],
            "entries": list(reversed(self.recent_lines)),
            "tokens": self.tokens,
            "tokens_today": self.tokens_today,
        }
        if self.pending_prompt:
            snap["prompt"] = {
                "id": self.pending_prompt.tool_use_id,
                "tool": self.pending_prompt.tool,
                "summary": self.pending_prompt.summary,
                "hint": self.pending_prompt.hint,
            }
        if now < self.notify_flash_until:
            snap["completed"] = True
        return snap


# ─────────────────────────── BLE side ────────────────────────────

async def find_device(timeout: float = 15.0):
    log("scanning for Claude-* device...")

    def matcher(dev, advdata):
        name = advdata.local_name or dev.name or ""
        return name.startswith(DEVICE_NAME_PREFIX)

    return await BleakScanner.find_device_by_filter(matcher, timeout=timeout)


async def ble_send(state: State, client: BleakClient, obj: dict) -> None:
    """Write a single JSON object terminated by \n, chunked under MTU."""
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    chunk = max(20, min(180, (client.mtu_size or 23) - 3))
    for i in range(0, len(line), chunk):
        await client.write_gatt_char(NUS_RX, line[i : i + chunk], response=False)
        await asyncio.sleep(0.005)


async def handle_device_msg(state: State, client: BleakClient, obj: dict) -> None:
    cmd = obj.get("cmd")
    if cmd == "permission":
        req_id = obj.get("id", "")
        decision = obj.get("decision", "")
        if state.pending_prompt and state.pending_prompt.tool_use_id == req_id:
            if not state.pending_prompt.future.done():
                if decision == "once":
                    state.pending_prompt.future.set_result("allow")
                elif decision == "deny":
                    state.pending_prompt.future.set_result("deny")
                else:
                    state.pending_prompt.future.set_result("abstain")
                log(f"device decision: {decision} for {req_id[:8]}")
        else:
            log(f"ignored stale permission resp for req {req_id[:8]}")
    elif cmd == "status":
        await ble_send(state, client, {
            "ack": "status", "ok": True,
            "data": {"name": "claude-code-bridge", "sec": True},
        })
    elif cmd in ("name", "owner", "unpair"):
        await ble_send(state, client, {"ack": cmd, "ok": True})


async def ble_session(state: State, client: BleakClient) -> None:
    global ble_connected
    ble_connected = True
    rx = bytearray()

    async def on_notify(_handle: int, data: bytearray) -> None:
        rx.extend(data)
        while b"\n" in rx:
            line, _, rest = bytes(rx).partition(b"\n")
            rx.clear()
            rx.extend(rest)
            line = line.strip()
            if not line.startswith(b"{"):
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception as e:
                log(f"bad JSON from device: {e!r}")
                continue
            await handle_device_msg(state, client, obj)

    await client.start_notify(NUS_TX, on_notify)
    log(f"connected, mtu={client.mtu_size}")

    # One-shot on connect: time sync + owner
    tz = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone)
    await ble_send(state, client, {"time": [int(time.time()), tz]})
    owner = (os.environ.get("USER") or "you")[:20]
    await ble_send(state, client, {"cmd": "owner", "name": owner})

    # Periodic snapshot loop
    async def snapshot_loop() -> None:
        while True:
            try:
                await ble_send(state, client, state.snapshot())
            except Exception as e:
                log(f"snapshot send failed: {e!r}")
                return
            try:
                await asyncio.wait_for(state.dirty.wait(), timeout=SNAPSHOT_INTERVAL)
                state.dirty.clear()
                await asyncio.sleep(0.05)  # debounce burst events
            except asyncio.TimeoutError:
                pass

    snap_task = asyncio.create_task(snapshot_loop())
    try:
        while client.is_connected:
            await asyncio.sleep(1.0)
    finally:
        ble_connected = False
        # If a prompt was pending, abort it so the hook doesn't hang
        if state.pending_prompt and not state.pending_prompt.future.done():
            state.pending_prompt.future.set_result("abstain")
            log("BLE disconnected, aborting pending prompt")
        snap_task.cancel()
        with contextlib.suppress(BaseException):
            await snap_task


async def ble_loop(state: State) -> None:
    while True:
        try:
            dev = await find_device(timeout=15)
            if not dev:
                log("no device found, retry in 10s")
                await asyncio.sleep(10)
                continue
            log(f"found {dev.name} ({dev.address}), connecting...")
            async with BleakClient(dev, timeout=20) as client:
                await ble_session(state, client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"BLE error: {e!r}, retry in 5s")
            await asyncio.sleep(5)


# ─────────────────────────── hook server ────────────────────────────

def read_transcript_tokens_delta(state: State, path: str) -> int:
    """Return new output_tokens since last time we read this transcript.

    We re-read the whole file (Claude Code transcripts are JSONL, append-only,
    typically <1MB), sum output_tokens, and return the delta from what we
    already counted for this path. Cheap and resilient to log rotation since
    a new path means we start fresh.
    """
    try:
        p = Path(path)
        if not p.exists():
            return 0
        total = 0
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    total += int(usage.get("output_tokens") or 0)
        last = state.transcript_seen.get(path, 0)
        if total < last:
            # path reused or truncated — reset baseline
            last = 0
        state.transcript_seen[path] = total
        return max(0, total - last)
    except Exception as e:
        log(f"transcript read failed: {e!r}")
        return 0


async def handle_hook(state: State, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await reader.readline()
        if not data:
            return
        evt = json.loads(data.decode("utf-8"))
    except Exception as e:
        log(f"hook recv error: {e!r}")
        writer.close()
        await writer.wait_closed()
        return

    kind = evt.get("event", "")
    sid = evt.get("session_id", "")

    # PreToolUse is request-response: keep the connection open
    if kind == "PreToolUse":
        await handle_pre_tool_use(state, evt, writer)
        return

    # Everything else is fire-and-forget
    try:
        if kind == "SessionStart":
            state.active_sessions.add(sid)
            state.add_line(f"{datetime.now():%H:%M} session start")
            state.dirty.set()

        elif kind == "PostToolUse":
            state.active_sessions.add(sid)
            tool = evt.get("tool", "")
            if tool:
                state.add_line(f"{datetime.now():%H:%M} {tool}")
                state.dirty.set()

        elif kind == "Stop":
            tp = evt.get("transcript_path", "")
            if tp:
                added = read_transcript_tokens_delta(state, tp)
                if added > 0:
                    state.tokens += added
                    state.tokens_today += added
                    state.dirty.set()
                    log(f"Stop: +{added} tokens (total {state.tokens})")

        elif kind == "SessionEnd":
            state.active_sessions.discard(sid)
            state.dirty.set()

        elif kind == "Notification":
            msg = (evt.get("msg") or "")[:23]
            if msg:
                now = time.time()
                state.add_line(f"{datetime.now():%H:%M} {msg}")
                state.notify_msg = msg
                state.notify_msg_until = now + NOTIFY_MSG_S
                state.notify_flash_until = now + NOTIFY_FLASH_S
                state.dirty.set()
                log(f"Notification: {msg!r} → buzz {NOTIFY_FLASH_S}s")

        else:
            log(f"unknown event: {kind}")

    except Exception as e:
        log(f"hook handler error ({kind}): {e!r}")

    writer.close()
    with contextlib.suppress(BaseException):
        await writer.wait_closed()


async def handle_pre_tool_use(state: State, evt: dict, writer: asyncio.StreamWriter) -> None:
    """Handle PreToolUse: push prompt to device, wait for button press, respond."""
    tool_use_id = evt.get("tool_use_id", "")
    tool = evt.get("tool", "")
    summary = evt.get("summary", tool)
    hint = evt.get("hint", "")

    # If already handling a prompt or BLE is down, abstain immediately
    if state.pending_prompt or not ble_connected:
        reason = "busy" if state.pending_prompt else "no BLE"
        log(f"PreToolUse abstain ({reason}): {tool}")
        response = json.dumps({"decision": "abstain"}) + "\n"
        writer.write(response.encode())
        await writer.drain()
        writer.close()
        with contextlib.suppress(BaseException):
            await writer.wait_closed()
        return

    # Create pending prompt and push to device
    prompt = PendingPrompt(tool_use_id, tool, summary, hint)
    state.pending_prompt = prompt
    state.dirty.set()  # trigger immediate snapshot with prompt data
    log(f"PreToolUse pending: {tool} ({tool_use_id[:8]})")

    try:
        decision = await asyncio.wait_for(prompt.future, timeout=PROMPT_TIMEOUT)
    except asyncio.TimeoutError:
        decision = "abstain"
        log(f"PreToolUse timeout: {tool}")
    except asyncio.CancelledError:
        decision = "abstain"
    finally:
        state.pending_prompt = None
        state.dirty.set()  # clear prompt from next snapshot

    # Send decision back to hook
    if decision == "allow":
        response = {"decision": "allow"}
    elif decision == "deny":
        response = {"decision": "deny", "reason": "Denied from device"}
    else:
        response = {"decision": "abstain"}

    try:
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
    except Exception as e:
        log(f"PreToolUse response send failed: {e!r}")
    finally:
        writer.close()
        with contextlib.suppress(BaseException):
            await writer.wait_closed()


async def hook_server(state: State) -> None:
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_hook(state, r, w), path=SOCK_PATH
    )
    os.chmod(SOCK_PATH, 0o600)
    log(f"hook server listening on {SOCK_PATH}")
    async with server:
        await server.serve_forever()


# ─────────────────────────── main ────────────────────────────

async def amain() -> None:
    state = State()
    stop = asyncio.Event()

    def shutdown() -> None:
        log("shutting down...")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    ble_task = asyncio.create_task(ble_loop(state), name="ble")
    hook_task = asyncio.create_task(hook_server(state), name="hook")

    await stop.wait()

    for t in (ble_task, hook_task):
        t.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.gather(ble_task, hook_task, return_exceptions=True)

    if os.path.exists(SOCK_PATH):
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
