# Yaldabaoth

Full desktop control for AI agents. Keyboard, mouse, OCR, and vision â€” gives any MCP-compatible AI (Claude, Cursor, etc.) the ability to operate Windows like a human does.

Most desktop automation tools give the AI a screenshot and pray. Yaldabaoth is smarter: it starts with structured UI data (Windows UI Automation) for speed, adds OCR for reading what's on screen, and falls back to vision-guided mouse clicks (via Gemini) when nothing else works. The AI decides which tool fits the situation.

## What it does

Two files, full desktop control:

| File | Runs on | Purpose |
|------|---------|---------|
| `virtual_keyboard_mcp.py` | Anywhere (Python) | MCP server â€” exposes 13 `vk_*` tools for keyboard, observation, OCR, and vision |
| `desktop_control.py` | Windows | Daemon â€” executes keyboard input (SendInput), reads UI Automation state, OCR, Gemini vision, serves health on `localhost:8765` |

The MCP server communicates with the daemon through a JSON file bridge (`command.json` / `command.result.json`). No network required between them.

## Requirements

- **Python 3.10+** (uses `match`/`case` and PEP 604 union types)
- **Windows 10/11** (daemon uses Win32 SendInput and COM-based UI Automation)

## Setup

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Start the daemon

```
cd path\to\Yaldabaoth
python desktop_control.py --allow-input
```

You should see:
```
Yaldabaoth listening on http://127.0.0.1:8765 allow_input=True command_file=...\command.json
```

### 3. Add MCP to your AI tool

Add this to your Claude Desktop config (`claude_desktop_config.json`) or Claude Code config (`~/.claude.json`):

```json
{
  "mcpServers": {
    "yaldabaoth": {
      "command": "python",
      "args": ["path/to/Yaldabaoth/virtual_keyboard_mcp.py"],
      "env": {
        "VK_COMMAND_PATH": "path/to/Yaldabaoth/command.json",
        "VK_RESULT_PATH": "path/to/Yaldabaoth/command.result.json",
        "VK_HOST_LAUNCH": "pythonw path/to/Yaldabaoth/desktop_control.py --allow-input"
      }
    }
  }
}
```

The `VK_HOST_LAUNCH` env var tells the MCP server how to auto-start the daemon if it's not running.

## Tools

### Keyboard

#### `vk_hotkey(keys: str)`
Press a hotkey chord. Examples: `"win r"`, `"ctrl l"`, `"alt f4"`, `"ctrl shift t"`

#### `vk_press(key: str)`
Press a single key. Examples: `"enter"`, `"tab"`, `"escape"`, `"left"`

#### `vk_type(text: str)`
Type literal text into the focused input. Handles Ctrl/Alt modifiers and Unicode fallback.

### Navigation

#### `vk_sweep(stop_if?, compact=True)`
Enumerate all focusable elements via Tab key. Returns name + role for each. Use `stop_if` with `activate_on_match=True` to find and activate a specific element.

#### `vk_route(steps: list[dict])`
Execute a multi-step keyboard sequence atomically. Each step is one of:
```json
[
  {"hotkey": "win r"},
  {"sleep": 0.3},
  {"type": "notepad"},
  {"press": "enter"},
  {"sleep": 0.5},
  {"type": "Hello from Yaldabaoth"}
]
```

### Observation

#### `vk_observe_window(windows: bool = False)`
Returns the active foreground window (process, title, rect). Pass `windows=True` for all visible windows.

#### `vk_observe_focus()`
Returns the focused UI element via UI Automation: name, control type, automation ID, rect, keyboard focus state.

#### `vk_snapshot()`
Screenshot + full UIA state. Returns the screenshot path for vision-capable models.

#### `vk_status()`
Returns bridge health: daemon connectivity, file paths, last result.

### Vision & OCR

#### `vk_click(target: str)`
OCR the screen and click on matching text. Finds the target string, moves the mouse to it, and clicks.

#### `vk_ocr()`
OCR the entire screen. Returns all visible text with bounding box positions.

#### `vk_mouse_move(x: int, y: int)`
Move the mouse cursor to specific screen coordinates.

#### `vk_vision(goal: str)`
Screenshot to Gemini browser, returns structured elements with pixel coordinates.

#### `vk_gemini_click(goal: str)`
Vision-guided click: screenshot to Gemini, parse coordinates, move mouse and click.

## Architecture

```
AI Agent (Claude, Cursor, etc.)
    |
    |  MCP protocol (stdio)
    v
virtual_keyboard_mcp.py          <- runs as MCP server
    |
    |  file bridge (command.json -> command.result.json)
    v
desktop_control.py               <- runs on Windows desktop
    |
    |-- SendInput (keyboard)
    |-- UI Automation (observation)
    |-- RapidOCR (text recognition)
    '-- Gemini vision (element detection)
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VK_COMMAND_PATH` | Yes | -- | Path to `command.json` (file bridge input) |
| `VK_RESULT_PATH` | Yes | -- | Path to `command.result.json` (file bridge output) |
| `VK_HOST_LAUNCH` | No | -- | Shell command to auto-start the daemon |
| `VK_TIMEOUT` | No | `30` | Default timeout in seconds for commands |
| `VK_HEALTH_URL` | No | `http://127.0.0.1:8765/health` | Daemon health endpoint |
| `VK_HOST_START_TIMEOUT` | No | `8` | Seconds to wait for daemon after auto-start |

## Security

**`--allow-input` grants full keyboard control over your desktop.** When this flag is set:

- Any process that can write to `command.json` or reach `localhost:8765` can inject arbitrary keystrokes
- The HTTP server binds to `127.0.0.1` only (not exposed to the network), but any local process can call it
- The file bridge has no authentication -- security comes from filesystem permissions on `command.json`
- Do not run on shared or multi-user machines without understanding the implications

Without `--allow-input`, the daemon is read-only: it can observe windows and UI state but cannot send any input.

## AutoResearch: Self-Improving Desktop Navigation

Yaldabaoth includes an autonomous research loop inspired by [Karpathy's autoresearch pattern](https://github.com/karpathy/autoresearch). Instead of optimizing ML metrics, it optimizes desktop navigation strategies.

### How it works

1. **`program.md`** defines research goals (e.g., "learn to open Chrome reliably")
2. **`research_loop.py`** executes experiments through Yaldabaoth's desktop control
3. **`strategies.json`** stores what worked (approaches with success rates)
4. **`experiment_log.jsonl`** records every attempt for analysis
5. **Git** serves as the ratchet -- successful strategies are committed, failures are not

### The Ratchet

Like the original autoresearch, knowledge only moves forward:
- **Successful experiments**: commit `strategies.json` (knowledge grows)
- **Failed experiments**: log the failure, do not commit (knowledge preserved)
- **Over time**: approaches with >95% success rate over 20+ attempts are marked "proven"
- **Over time**: approaches with >80% failure rate over 10+ attempts are pruned

### MCP Tools for Learning

| Tool | Purpose |
|------|---------|
| `vk_recall(task)` | Look up the best known strategy for a task |
| `vk_learn(task, approach_id, steps, success)` | Record an attempt result |
| `vk_experiment(task)` | Run one iteration of the research loop |

### Running the loop

```bash
python research_loop.py                  # continuous loop
python research_loop.py --once           # single experiment
python research_loop.py --max 50         # run 50 experiments then stop
```

### Karpathy Principles (from [andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills))

The following principles are embedded in `CLAUDE.md` to guide AI behavior:

1. **Think before acting** -- always check `vk_recall()` for a proven strategy before trying something new
2. **Simplicity first** -- prefer keyboard shortcuts over mouse clicks, short routes over long ones
3. **Surgical changes** -- change one thing per experiment, record what changed
4. **Goal-driven execution** -- always verify the outcome with `vk_observe_window()` or `vk_ocr()`

## Design principles

- **Keyboard first, mouse when needed.** Tab to enumerate, Enter to activate. When that's not enough, OCR-click or vision-click fills the gap.
- **UIA, not just pixels.** Structured element data (name, type, rect) for speed. OCR and Gemini vision for everything UIA can't see.
- **File bridge, not HTTP.** The primary transport is atomic file writes. 5ms polling. HTTP is optional for health checks.
- **No secrets in transit.** Everything is local -- no API keys, no cloud, no network between MCP and daemon.
- **AI is the reasoner.** The daemon is a dumb I/O layer. The AI reads sweep output, decides what to do, and acts.
- **Learn from mistakes.** Every desktop action is recorded. Strategies that work get committed; strategies that fail get pruned.
