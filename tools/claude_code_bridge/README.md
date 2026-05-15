# Claude Code → Hardware Buddy bridge

Drives a `Claude-*` BLE device from Claude Code CLI directly, without the
Claude desktop app. Supports **device-side permission decisions**: when
Claude Code needs approval for a tool use, the device shows a full-screen
approval UI with tool name, summary, and command details. You approve or
deny from the physical buttons — no need to switch back to the terminal.

## Architecture

```
Claude Code (terminal) ─▶ hook.py ─▶ Unix socket ─▶ daemon.py ─▶ BLE ─▶ M5StickC
                                                        ◀── button press ──┘
```

- **`daemon.py`** — long-running process. Maintains the BLE connection,
  accumulates session state, pushes snapshots to the device, relays
  device button presses back to the hook.
- **`hook.py`** — short-lived script spawned by Claude Code per hook event.
  For `PreToolUse`: sends the request to the daemon, blocks until the
  device responds (approve/deny), then returns the decision to Claude Code.
  For other events: fire-and-forget status update.

## Features

- **Full-screen approval UI** — hides the pet, shows tool name (large colored
  banner), human-readable summary, and full command/code with diff coloring
- **Tool category colors** — Bash (magenta), Edit/Write (amber), Read/Grep (blue),
  Plan (teal), others (orange)
- **Diff highlighting** — removed lines in red, added lines in green
- **IMU tilt** — pet position responds to device tilt via accelerometer
- **Auto-allow reads** — Read/Grep/Glob bypass the device for speed
- **Deny animation** — pet shows dizzy reaction on deny, heart on fast approve

## Quick Start

### 1. Flash the firmware

```bash
# From the project root
pio run -t upload
```

### 2. Install Python dependencies

```bash
cd tools/claude_code_bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Pair the device via Bluetooth

1. Wake the device (press any button)
2. Confirm Bluetooth is on: long-press A → Settings → Bluetooth → ON
3. Start the daemon (see next step) — it will scan for `Claude-XXXX` devices
4. On first connection, the device screen shows a **6-digit passkey** (large centered number)
5. macOS will pop a Bluetooth pairing dialog — enter the 6 digits shown on the device
6. Once paired, future reconnects are automatic (no passkey needed)

If macOS doesn't show the pairing dialog, go to **System Settings → Bluetooth**,
find `Claude-XXXX`, and click **Connect** — the passkey prompt will appear there.

To re-pair (new computer or after factory reset):
- Remove the old `Claude-XXXX` from macOS Bluetooth settings
- On device: long-press A → Settings → Reset → Factory Reset → tap twice
- Restart the daemon and pair again

### 4. Start the daemon

```bash
./run-daemon.sh
```

You should see:
```
[12:34:01] scanning for Claude-* device...
[12:34:03] found Claude-A4B7 (...), connecting...
[12:34:04] connected, mtu=515
[12:34:04] hook server listening on /tmp/claude-buddy.sock
```

### 5. Configure Claude Code hooks

Edit `~/.claude/settings.json` (or your project's `.claude/settings.json`).
Replace `/path/to/` with the actual absolute path where you cloned this repo:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [{
          "type": "command",
          "command": "/path/to/claude-desktop-buddy/tools/claude_code_bridge/hook.py",
          "timeout": 180000
        }]
      }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "command": "/path/to/claude-desktop-buddy/tools/claude_code_bridge/hook.py" }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "/path/to/claude-desktop-buddy/tools/claude_code_bridge/hook.py" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "/path/to/claude-desktop-buddy/tools/claude_code_bridge/hook.py" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "/path/to/claude-desktop-buddy/tools/claude_code_bridge/hook.py" }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command", "command": "/path/to/claude-desktop-buddy/tools/claude_code_bridge/hook.py" }] }
    ]
  }
}
```

**Important:** The `PreToolUse` hook with `timeout: 180000` (3 minutes) is
what enables device-side approval. Without it, you only get status mirroring.

### 6. (Optional) Auto-generate hook paths

Run this from the repo root to print the correct JSON with your actual paths:

```bash
HOOK="$(pwd)/tools/claude_code_bridge/hook.py"
echo "Use this path in your settings.json hooks:"
echo "  $HOOK"
```

## Permission Flow

```
┌─────────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ Claude Code │────▶│ hook.py  │────▶│ daemon   │────▶│  Device  │
│  PreToolUse │     │ (blocks) │     │          │     │          │
└─────────────┘     └──────────┘     └──────────┘     └──────────┘
                         ▲                                   │
                         │         ┌──────────┐              │
                         └─────────│  daemon  │◀─────────────┘
                      (decision)   │          │    (BtnA=approve
                                   └──────────┘     BtnB=deny)
```

- **Read / Grep / Glob** → auto-allowed in hook.py, never hits the device
- **Bash / Edit / Write / others** → shown on device, waits for button press
- **Timeout (120s)** → abstains, falls back to CLI terminal prompt

## Customizing Auto-Allow

Edit `hook.py` line 79 to change which tools bypass the device:

```python
if tool in ("Read", "Grep", "Glob"):
```

Add or remove tool names as needed. For example, to also auto-allow
`WebSearch`:

```python
if tool in ("Read", "Grep", "Glob", "WebSearch"):
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BUDDY_SOCK` | `/tmp/claude-buddy.sock` | Unix socket path for hook↔daemon communication |

## Troubleshooting

### Device not found during scan

- Wake the device (any button press) — BLE radio sleeps with the screen
- Check Settings → Bluetooth → ON on the device
- If previously paired with Claude desktop app: long-hold A → settings →
  reset → factory reset

### "daemon unreachable" in hook stderr

- Daemon isn't running, or socket path mismatch
- Confirm `/tmp/claude-buddy.sock` exists when daemon is up
- Set `CLAUDE_BUDDY_SOCK` in both daemon and hook env if customized

### Approval screen not showing

- Ensure `PreToolUse` hook is configured with `timeout: 180000`
- Check daemon log — if it says "abstain (no BLE)", the device is disconnected
- If it says "abstain (busy)", a previous prompt is still pending

### Hook not firing

```bash
claude --debug
```

Check for Python tracebacks. Verify settings.json syntax:
```bash
python3 -c "import json; json.load(open('$HOME/.claude/settings.json'))"
```

### macOS Bluetooth passkey dialog

First connection after factory reset prompts for the 6-digit passkey shown
on the device screen. After bonding, reconnects are silent. If it keeps
prompting, remove the device from System Settings → Bluetooth and re-pair.

## Notes

- The daemon doesn't persist token counts across restarts
- Multiple concurrent Claude Code sessions show aggregate state on device
- The device's `once` decision means "allow this one time" — each new tool
  use requires a fresh approval
