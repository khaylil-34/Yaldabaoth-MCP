#!/usr/bin/env python3
r"""Yaldabaoth MCP -- desktop control for AI agents.

Exposes keyboard and UI Automation tools via MCP. Sends route commands to the
Yaldabaoth desktop daemon (desktop_control.py) through a JSON file bridge.

Tools: vk_hotkey, vk_press, vk_type, vk_route, vk_observe_window,
       vk_observe_focus, vk_sweep, vk_snapshot, vk_status,
       vk_recall, vk_learn, vk_experiment
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from yald_bridge import YaldBridge

mcp = FastMCP("virtual-keyboard")

COMMAND_PATH = Path(os.environ["VK_COMMAND_PATH"])
RESULT_PATH = Path(os.environ["VK_RESULT_PATH"])
DEFAULT_TIMEOUT = float(os.environ.get("VK_TIMEOUT", "30.0"))
HEALTH_URL = os.environ.get("VK_HEALTH_URL", "http://127.0.0.1:8765/health")
HOST_LAUNCH = os.environ.get("VK_HOST_LAUNCH", "").strip()
HOST_START_TIMEOUT = float(os.environ.get("VK_HOST_START_TIMEOUT", "8.0"))

SCRIPT_DIR = Path(__file__).resolve().parent
STRATEGIES_PATH = Path(os.environ.get(
    "YALD_STRATEGIES_PATH", str(SCRIPT_DIR / "strategies.json")))
PROGRAM_PATH = Path(os.environ.get(
    "YALD_PROGRAM_PATH", str(SCRIPT_DIR / "program.md")))
LOG_PATH = Path(os.environ.get(
    "YALD_LOG_PATH", str(SCRIPT_DIR / "experiment_log.jsonl")))

HOST_BOOTSTRAP: dict[str, Any] = {"checked": False}

bridge = YaldBridge(COMMAND_PATH, RESULT_PATH, DEFAULT_TIMEOUT)


def _probe_health(timeout: float = 0.75) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def _spawn_host(launch_cmd: str) -> dict[str, Any]:
    try:
        argv = shlex.split(launch_cmd, posix=False)
        cleaned = []
        for tok in argv:
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
                cleaned.append(tok[1:-1])
            else:
                cleaned.append(tok)
        subprocess.Popen(
            cleaned,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "argv": cleaned}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _ensure_host() -> None:
    initial = _probe_health()
    if initial is not None:
        HOST_BOOTSTRAP.update({"checked": True, "initial": "up", "health": initial, "spawned": False})
        return
    if not HOST_LAUNCH:
        HOST_BOOTSTRAP.update({
            "checked": True, "initial": "down", "spawned": False,
            "hint": "Host not running. Set VK_HOST_LAUNCH to enable autostart, or start the daemon manually.",
        })
        return
    spawn = _spawn_host(HOST_LAUNCH)
    deadline = time.time() + HOST_START_TIMEOUT
    final = None
    while time.time() < deadline:
        final = _probe_health()
        if final is not None:
            break
        time.sleep(0.2)
    HOST_BOOTSTRAP.update({
        "checked": True,
        "initial": "down",
        "spawned": True,
        "spawn_result": spawn,
        "health": final,
        "final": "up" if final else "still-down",
    })


_ensure_host()


@mcp.tool()
def vk_status() -> dict[str, Any]:
    """Return virtual keyboard bridge status and last result file content."""
    last = None
    if RESULT_PATH.exists():
        try:
            last = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            last = {"error": str(e)}
    live_health = _probe_health()
    return {
        "ok": True,
        "command_path": str(COMMAND_PATH),
        "result_path": str(RESULT_PATH),
        "command_path_exists": COMMAND_PATH.exists(),
        "result_path_exists": RESULT_PATH.exists(),
        "host_health_url": HEALTH_URL,
        "host_up": live_health is not None,
        "host_health": live_health,
        "host_bootstrap": HOST_BOOTSTRAP,
        "last_result": last,
    }


@mcp.tool()
def vk_hotkey(keys: str, wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Press a hotkey chord, e.g. 'win', 'win r', 'ctrl l', 'alt f4'."""
    return bridge.route([{"hotkey": keys}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_press(key: str, wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Press a single key, e.g. 'enter', 'tab', 'escape', 'left'."""
    return bridge.route([{"press": key}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_type(text: str, wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Type text into the active Windows input target."""
    return bridge.route([{"type": text}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_route(steps: list[dict[str, Any]], wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Execute a compact keyboard route locally in Windows.

    Step types: hotkey, type, press, sleep, press_if_focus, ensure_fullscreen,
    tab_sweep, arrow_sweep, focus_document, capture_screen, snapshot,
    focus, observe, wait_until.
    """
    if not isinstance(steps, list):
        return {"ok": False, "error": "steps must be a list"}
    return bridge.route(steps, wait=wait, timeout=timeout)


@mcp.tool()
def vk_observe_window(windows: bool = False, cached: bool = True, max_age_ms: int = 500,
                      wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Observe Windows foreground focus. Returns active window/process/title/rect; optionally visible windows."""
    return bridge.observe(windows=windows, uia=False, cached=cached, max_age_ms=max_age_ms, wait=wait, timeout=timeout)


@mcp.tool()
def vk_observe_focus(windows: bool = False, cached: bool = True, max_age_ms: int = 500,
                     wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Observe current Windows focus plus semantic UI/tab focus via UI Automation when available."""
    return bridge.observe(windows=windows, uia=True, cached=cached, max_age_ms=max_age_ms, wait=wait, timeout=timeout)


@mcp.tool()
def vk_sweep(key: str = "tab", max_steps: int = 80,
             stop_if: dict[str, Any] | None = None,
             activate_on_match: bool = False, activation_key: str = "enter",
             compact: bool = True,
             wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Enumerate focusable elements. compact=True returns name+role only."""
    return bridge.route([{"tab_sweep": {
        "key": key, "max_steps": max_steps, "stop_on_cycle": True,
        "stop_if": stop_if or {}, "activate_on_match": activate_on_match,
        "activation_key": activation_key, "compact": compact,
    }}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_snapshot(wait: bool = True,
                timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Screenshot + full UIA state + domain hint. Read the screenshot with vision."""
    return bridge.route([{"snapshot": {}}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_click(target: str, wait: bool = True, timeout: float = 30.0) -> dict[str, Any]:
    """Find visible text/element on screen and click it. Uses UIA first (<150ms), falls back to screen reader (<500ms)."""
    return bridge.route([{"click_target": {"target": target}}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_ocr(wait: bool = True, timeout: float = 30.0) -> dict[str, Any]:
    """Read all visible text on screen with positions. GPU-accelerated, 100-300ms."""
    return bridge.route([{"ocr_scan": {}}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_locate(target: str, wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Find target on screen and return 8-way direction from mouse.

    First call finds via UIA/screen reader (100-500ms) and caches.
    Repeat calls for same target use cache (<5ms) â€” use for mouse correction loops.
    Returns: direction (N/NE/E/SE/S/SW/W/NW/on_target), distance_px, center coords.
    """
    return bridge.route([{"locate_target": {"target": target}}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_mouse_move(x: int, y: int, wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Move mouse cursor to screen coordinates."""
    return bridge.route([{"mouse_move": {"x": x, "y": y}}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_vision(goal: str, prompt: str = "",
              wait: bool = True, timeout: float = 45.0) -> dict[str, Any]:
    """Screenshot -> Gemini vision -> structured elements with pixel coords. ~15s, zero API cost."""
    spec: dict[str, Any] = {"goal": goal}
    if prompt:
        spec["prompt"] = prompt
    return bridge.route([{"vision_query": spec}], wait=wait, timeout=timeout)


@mcp.tool()
def vk_gemini_click(goal: str,
                    wait: bool = True, timeout: float = 45.0) -> dict[str, Any]:
    """Vision-guided click: screenshot -> Gemini -> parse coords -> mouse click."""
    return bridge.route([{"gemini_click": {"goal": goal}}], wait=wait, timeout=timeout)


# ---------------------------------------------------------------------------
# AutoResearch tools -- strategy memory and experiment loop
# ---------------------------------------------------------------------------

@mcp.tool()
def vk_recall(task: str, tags: list[str] | None = None) -> dict[str, Any]:
    """Recall the best known strategy for a desktop task.

    Returns the highest success-rate approach, relevant anti-patterns,
    and site knowledge. Call this before attempting a new task.
    """
    from strategy_engine import (
        load_strategies, find_strategy, get_best_approach, get_anti_patterns,
        get_tool_preferences,
    )
    data = load_strategies(STRATEGIES_PATH)
    strategy = find_strategy(data, task, tags)
    keywords = tags or task.lower().split()
    tool_prefs = get_tool_preferences(data, keywords)
    if not strategy:
        return {"ok": True, "found": False, "task": task,
                "tool_preferences": tool_prefs}
    best = get_best_approach(strategy)
    return {
        "ok": True,
        "found": True,
        "task": task,
        "strategy": strategy.get("description", ""),
        "best_approach": best,
        "total_approaches": len(strategy.get("approaches", [])),
        "anti_patterns": get_anti_patterns(data, keywords),
        "tool_preferences": tool_prefs,
    }


@mcp.tool()
def vk_learn(task: str, approach_id: str, steps: list[dict[str, Any]],
             success: bool, duration_ms: float = 0,
             failure_reason: str = "") -> dict[str, Any]:
    """Record a strategy attempt result. Updates the knowledge base.

    Call after attempting a desktop task to record whether the approach worked.
    Over time this builds a knowledge base of effective strategies.
    """
    from strategy_engine import (
        load_strategies, save_strategies, create_strategy,
        add_approach, record_attempt,
    )
    data = load_strategies(STRATEGIES_PATH)
    strategy_key = task.lower().replace(" ", "_")

    if strategy_key not in data.get("strategies", {}):
        create_strategy(data, strategy_key, task)

    existing_ids = {
        a["id"] for a in data["strategies"][strategy_key].get("approaches", [])
    }
    if approach_id not in existing_ids:
        add_approach(data, strategy_key, {"id": approach_id, "steps": steps})

    record_attempt(data, strategy_key, approach_id, success, duration_ms, failure_reason)
    save_strategies(STRATEGIES_PATH, data)

    return {
        "ok": True,
        "strategy": strategy_key,
        "approach": approach_id,
        "success": success,
        "recorded": True,
    }


@mcp.tool()
def vk_experiment(task: str,
                  verification: dict[str, Any] | None = None,
                  timeout: float = 60.0) -> dict[str, Any]:
    """Run a single research experiment for a desktop task.

    Selects the best known approach (or uses seed steps), executes it,
    verifies the result, and records the outcome. One iteration of the
    Karpathy ratchet loop -- successful experiments are git-committed.
    """
    from research_loop import ResearchLoop
    loop = ResearchLoop(bridge, STRATEGIES_PATH, PROGRAM_PATH, LOG_PATH)
    result = loop.run_single(task, loop._infer_steps(task) or [],
                             verification or {})
    return result.to_dict()


if __name__ == "__main__":
    mcp.run()
