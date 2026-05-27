#!/usr/bin/env python3
"""
Yaldabaoth -- desktop control daemon.

Turns the Windows desktop into a callable control plane for AI agents.
Keyboard input (SendInput), UI Automation observation, and element enumeration.

Default safety: input endpoints are disabled unless started with --allow-input.
Read-only endpoints (/health, /windows, /active) always work.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- Timing defaults (overridable via environment) ----------------------------
TIMING = {
    "tick_ms":         float(os.environ.get("VK_TICK_MS", "150")),
    "key_hold_ms":     float(os.environ.get("VK_KEY_HOLD_MS", "8")),
    "poll_ms":         float(os.environ.get("VK_POLL_MS", "5")),
    "press_hold_ms":   float(os.environ.get("VK_PRESS_HOLD_MS", "40")),
    "tap_hold_ms":     float(os.environ.get("VK_TAP_HOLD_MS", "8")),
    "hotkey_gap_ms":   float(os.environ.get("VK_HOTKEY_GAP_MS", "40")),
    "type_gap_ms":     float(os.environ.get("VK_TYPE_GAP_MS", "15")),
    "focus_sample_ms": float(os.environ.get("VK_FOCUS_SAMPLE_MS", "15")),
}

# ---- comtypes UIA singleton -------------------------------------------------
_uia = None
_uia_mod = None
_uia_control_type_names = {
    50000: "ControlType.Button", 50001: "ControlType.Calendar",
    50002: "ControlType.CheckBox", 50003: "ControlType.ComboBox",
    50004: "ControlType.Edit", 50005: "ControlType.Hyperlink",
    50006: "ControlType.Image", 50007: "ControlType.ListItem",
    50008: "ControlType.List", 50009: "ControlType.Menu",
    50010: "ControlType.MenuBar", 50011: "ControlType.MenuItem",
    50012: "ControlType.ProgressBar", 50013: "ControlType.RadioButton",
    50014: "ControlType.ScrollBar", 50015: "ControlType.Slider",
    50016: "ControlType.Spinner", 50017: "ControlType.StatusBar",
    50018: "ControlType.Tab", 50019: "ControlType.TabItem",
    50020: "ControlType.Text", 50021: "ControlType.ToolBar",
    50022: "ControlType.ToolTip", 50023: "ControlType.Tree",
    50024: "ControlType.TreeItem", 50025: "ControlType.Custom",
    50026: "ControlType.Group", 50027: "ControlType.Thumb",
    50028: "ControlType.DataGrid", 50029: "ControlType.DataItem",
    50030: "ControlType.Document", 50031: "ControlType.SplitButton",
    50032: "ControlType.Window", 50033: "ControlType.Pane",
    50034: "ControlType.Header", 50035: "ControlType.HeaderItem",
    50036: "ControlType.Table", 50037: "ControlType.TitleBar",
    50038: "ControlType.Separator", 50039: "ControlType.SemanticZoom",
    50040: "ControlType.AppBar",
}
_uia_localized_names = {
    50000: "button", 50001: "calendar", 50002: "check box", 50003: "combo box",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "list item",
    50008: "list", 50009: "menu", 50010: "menu bar", 50011: "menu item",
    50012: "progress bar", 50013: "radio button", 50014: "scroll bar",
    50015: "slider", 50016: "spinner", 50017: "status bar", 50018: "tab",
    50019: "tab item", 50020: "text", 50021: "tool bar", 50022: "tool tip",
    50023: "tree", 50024: "tree item", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "data grid", 50029: "data item", 50030: "document",
    50031: "split button", 50032: "window", 50033: "pane", 50034: "header",
    50035: "header item", 50036: "table", 50037: "title bar",
    50038: "separator", 50039: "semantic zoom", 50040: "app bar",
}
UIA_VALUE_PATTERN_ID = 10002
UIA_INVOKE_PATTERN_ID = 10000
UIA_IS_KEYBOARD_FOCUSABLE_PROPERTY_ID = 30009
UIA_IS_OFFSCREEN_PROPERTY_ID = 30022
UIA_TREE_SCOPE_DESCENDANTS = 4

try:
    import comtypes
    import comtypes.client
    _uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
    _uia = comtypes.client.CreateObject(
        "{ff48dba4-60ef-4201-aa87-54103eef594e}",
        interface=_uia_mod.IUIAutomation,
    )
    print("[UIA] comtypes UIA initialized (sub-1ms reads)")
except Exception as _uia_err:
    print(f"[UIA] comtypes unavailable ({_uia_err})")
    _uia = None

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
PROCESS_NAME_CACHE = {}
PROCESS_NAME_CACHE_TTL = 10.0

# ---- Win32 window inspection -------------------------------------------------

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
kernel32.GlobalFree.restype = wintypes.HANDLE

SW_RESTORE = 9


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _rect_dict(rect):
    return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top}


def _window_rect(hwnd: int):
    rect = wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return _rect_dict(rect)
    return None


def _window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    if user32.GetClassNameW(hwnd, buf, len(buf)):
        return buf.value
    return ""


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    now = time.time()
    cached = PROCESS_NAME_CACHE.get(pid)
    if cached and now - cached["ts"] < PROCESS_NAME_CACHE_TTL:
        return cached["name"]
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True, stderr=subprocess.DEVNULL, timeout=0.35,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).strip()
        if out and "No tasks" not in out:
            name = out.split(",", 1)[0].strip().strip('"')
            PROCESS_NAME_CACHE[pid] = {"name": name, "ts": now}
            return name
    except Exception:
        pass
    PROCESS_NAME_CACHE[pid] = {"name": "", "ts": now}
    return ""


def list_windows():
    windows = []

    @EnumWindowsProc
    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title.strip():
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append({
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "process": _process_name(int(pid.value)),
            "title": title,
            "class_name": _window_class(hwnd),
            "rect": _window_rect(hwnd),
        })
        return True

    user32.EnumWindows(callback, 0)
    return windows


def active_window():
    hwnd = int(user32.GetForegroundWindow())
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"hwnd": hwnd, "pid": int(pid.value), "process": _process_name(int(pid.value)),
            "title": _window_title(hwnd), "class_name": _window_class(hwnd), "rect": _window_rect(hwnd)}


def gui_thread_focus():
    active = active_window()
    if not active:
        return {"ok": False, "error": "no foreground window"}
    thread_id = user32.GetWindowThreadProcessId(wintypes.HWND(active["hwnd"]), None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return {"ok": False, "error": "GetGUIThreadInfo failed", "active": active}

    def hwnd_info(hwnd):
        h = int(hwnd or 0)
        if not h:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {"hwnd": h, "pid": int(pid.value), "process": _process_name(int(pid.value)),
                "title": _window_title(hwnd), "class_name": _window_class(hwnd), "rect": _window_rect(hwnd)}

    return {
        "ok": True, "active": active, "thread_id": int(thread_id),
        "hwndActive": hwnd_info(info.hwndActive), "hwndFocus": hwnd_info(info.hwndFocus),
        "hwndCaret": hwnd_info(info.hwndCaret), "caret_rect": _rect_dict(info.rcCaret),
        "flags": int(info.flags),
    }


# ---- UIA focused element (comtypes only) ------------------------------------

def uia_focused_element():
    if _uia is None:
        return {"ok": False, "error": "comtypes UIA not available"}
    try:
        element = _uia.GetFocusedElement()
        if element is None:
            return {"ok": False, "error": "no focused element"}
        ct = int(element.CurrentControlType)
        rect = element.CurrentBoundingRectangle
        value = ""
        try:
            pattern = element.GetCurrentPattern(UIA_VALUE_PATTERN_ID)
            if pattern is not None:
                value = str(pattern.CurrentValue or "")
        except Exception:
            pass
        return {
            "ok": True,
            "name": str(element.CurrentName or ""),
            "automation_id": str(element.CurrentAutomationId or ""),
            "class_name": str(element.CurrentClassName or ""),
            "framework_id": str(element.CurrentFrameworkId or ""),
            "control_type": _uia_control_type_names.get(ct, f"ControlType.{ct}"),
            "localized_control_type": _uia_localized_names.get(ct, str(element.CurrentLocalizedControlType or "")),
            "has_keyboard_focus": bool(element.CurrentHasKeyboardFocus),
            "is_keyboard_focusable": bool(element.CurrentIsKeyboardFocusable),
            "is_enabled": bool(element.CurrentIsEnabled),
            "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
                     "width": rect.right - rect.left, "height": rect.bottom - rect.top},
            "value": value,
        }
    except Exception as e:
        return {"ok": False, "error": f"comtypes UIA error: {e}"}


def sample_focus_fast():
    return {
        "ok": True,
        "active": active_window(),
        "focused_element": uia_focused_element() if _uia is not None else {"ok": False},
        "sampled_at": time.time(),
    }


def observe_state(include_windows: bool = False, include_uia: bool = True):
    state = {"ok": True, "active": active_window(), "gui_focus": gui_thread_focus()}
    if include_uia:
        state["focused_element"] = uia_focused_element()
    if include_windows:
        state["windows"] = list_windows()
    return state


# ---- Focus sampler -----------------------------------------------------------

FOCUS_CACHE = {"ok": False, "error": "not started"}
FOCUS_CACHE_LOCK = threading.Lock()
_FOCUS_SAMPLER_STARTED = False
_FOCUS_SAMPLER_BOOT_LOCK = threading.Lock()


class FocusSampler(threading.Thread):
    def __init__(self, interval: float = 0.01, include_uia: bool = True, fast: bool = True):
        super().__init__(daemon=True)
        self.interval = interval
        self.include_uia = include_uia
        self.fast = fast

    def run(self):
        global FOCUS_CACHE
        while True:
            started = time.time()
            try:
                if self.fast and self.include_uia:
                    state = sample_focus_fast()
                else:
                    state = observe_state(include_windows=False, include_uia=self.include_uia)
                state["sampled_at"] = started
                state["sample_age_ms"] = 0
            except Exception as e:
                state = {"ok": False, "error": str(e), "sampled_at": started, "sample_age_ms": 0}
            with FOCUS_CACHE_LOCK:
                FOCUS_CACHE = state
            elapsed = time.time() - started
            time.sleep(max(0.001, self.interval - elapsed))


def _ensure_focus_sampler():
    global _FOCUS_SAMPLER_STARTED
    if _FOCUS_SAMPLER_STARTED:
        return
    with _FOCUS_SAMPLER_BOOT_LOCK:
        if _FOCUS_SAMPLER_STARTED:
            return
        FocusSampler(interval=TIMING["focus_sample_ms"] / 1000.0, include_uia=True, fast=True).start()
        _FOCUS_SAMPLER_STARTED = True


def cached_focus_state(max_age_ms: int | None = None):
    _ensure_focus_sampler()
    with FOCUS_CACHE_LOCK:
        state = json.loads(json.dumps(FOCUS_CACHE, allow_nan=False, default=str))
    if state.get("sampled_at"):
        state["sample_age_ms"] = int((time.time() - float(state["sampled_at"])) * 1000)
    if max_age_ms is not None and state.get("sample_age_ms", 10**9) > max_age_ms:
        state["stale"] = True
        state["stale_error"] = f"focus sample stale > {max_age_ms}ms"
    return state


def focus_matches(state, name_contains: str | None = None, title_contains: str | None = None,
                  process_contains: str | None = None, control_type_contains: str | None = None):
    active = state.get("active") or {}
    elem = state.get("focused_element") or {}
    checks = []
    if name_contains:
        checks.append(name_contains.lower() in str(elem.get("name", "")).lower())
    if title_contains:
        checks.append(title_contains.lower() in str(active.get("title", "")).lower())
    if process_contains:
        checks.append(process_contains.lower() in str(active.get("process", "")).lower())
    if control_type_contains:
        checks.append(control_type_contains.lower() in str(elem.get("control_type", "")).lower())
    return all(checks) if checks else True


def focus_window(title_contains: str | None = None, hwnd: int | None = None):
    target = None
    if hwnd:
        target = hwnd
    elif title_contains:
        needle = title_contains.lower()
        for w in list_windows():
            if needle in w["title"].lower():
                target = w["hwnd"]
                break
    if not target:
        raise ValueError("No matching window found")
    user32.ShowWindow(target, SW_RESTORE)
    ok = bool(user32.SetForegroundWindow(target))
    time.sleep(0.15)
    return {"ok": ok, "active": active_window()}

# ---- Keyboard input ----------------------------------------------------------

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "pause": 0x13, "capslock": 0x14, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "meta": 0x5B,
    "menu": 0x5D,
}
for i in range(10):
    VK[str(i)] = 0x30 + i
for c in "abcdefghijklmnopqrstuvwxyz":
    VK[c] = ord(c.upper())
for i in range(1, 25):
    VK[f"f{i}"] = 0x70 + i - 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

EXTENDED_VKS = {0x5B, 0x5C, 0x5D, 0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E}

MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B}  # shift, ctrl, alt, win

user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short


def _vk(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1:
        return ord(n.upper())
    raise ValueError(f"Unknown key: {name}")


def _send_vk(vk: int, keyup: bool = False):
    flags = KEYEVENTF_KEYUP if keyup else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)))
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def _send_unicode_char(ch: str):
    code = ord(ch)
    inp_down = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(
        ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)))
    inp_up = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(
        ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)))
    user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
    time.sleep(0.001)
    user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))


def _release_all_modifiers():
    for vk in MODIFIER_VKS:
        try:
            _send_vk(vk, keyup=True)
        except Exception:
            pass


def key_down(vk: int):
    _send_vk(vk, keyup=False)


def key_up(vk: int):
    _send_vk(vk, keyup=True)


def press(key: str, hold_ms: float = TIMING["press_hold_ms"]):
    code = _vk(key)
    key_down(code)
    time.sleep(max(0.001, hold_ms / 1000.0))
    key_up(code)
    return {"ok": True, "pressed": key}


def tap(key: str, hold_ms: float = TIMING["tap_hold_ms"]):
    return press(key, hold_ms=hold_ms)


def hotkey(keys: str | list[str]):
    if isinstance(keys, str):
        parts = [p.strip() for p in keys.replace("+", " ").split() if p.strip()]
    else:
        parts = keys
    codes = [_vk(k) for k in parts]
    gap = TIMING["hotkey_gap_ms"] / 1000.0
    pressed = []
    try:
        for code in codes:
            key_down(code)
            pressed.append(code)
            time.sleep(gap)
    finally:
        for code in reversed(pressed):
            try:
                key_up(code)
            except Exception:
                pass
            time.sleep(gap)
    return {"ok": True, "hotkey": parts}


def type_text(text: str):
    gap = TIMING["type_gap_ms"] / 1000.0
    for ch in text:
        vk_scan = user32.VkKeyScanW(ch)
        if vk_scan == -1:
            _send_unicode_char(ch)
            time.sleep(gap)
            continue
        vk_code = vk_scan & 0xFF
        shift_state = (vk_scan >> 8) & 0xFF
        modifiers = []
        if shift_state & 1:
            modifiers.append(VK["shift"])
        if shift_state & 2:
            modifiers.append(VK["ctrl"])
        if shift_state & 4:
            modifiers.append(VK["alt"])
        try:
            for mod in modifiers:
                key_down(mod)
            key_down(vk_code)
            time.sleep(gap)
            key_up(vk_code)
        finally:
            for mod in reversed(modifiers):
                try:
                    key_up(mod)
                except Exception:
                    pass
        time.sleep(gap)
    return {"ok": True, "typed_len": len(text)}


# ---- Clipboard ---------------------------------------------------------------

def _read_clipboard_text(timeout_ms: int = 1000) -> str:
    CF_UNICODETEXT = 13
    deadline = time.time() + max(0.05, timeout_ms / 1000.0)
    last_error = "clipboard_unavailable"
    while time.time() < deadline:
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return ""
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = ctypes.windll.kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    ctypes.windll.kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        last_error = "open_clipboard_failed"
        time.sleep(0.025)
    raise RuntimeError(last_error)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _write_clipboard_image(img, timeout_ms: int = 2000) -> dict:
    """Put a PIL Image on the Windows clipboard as CF_DIB."""
    from PIL import Image
    started = time.time()
    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002

    flipped = img.transpose(Image.FLIP_TOP_BOTTOM).convert("RGB")
    w, h = flipped.size
    stride = (w * 3 + 3) & ~3
    raw = flipped.tobytes()
    row_len = w * 3
    pixels = bytearray()
    for row in range(h):
        for col in range(w):
            offset = (row * w + col) * 3
            r, g, b = raw[offset], raw[offset + 1], raw[offset + 2]
            pixels.extend((b, g, r))
        pixels.extend(b'\x00' * (stride - row_len))

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = h
    hdr.biPlanes = 1
    hdr.biBitCount = 24
    hdr.biCompression = 0
    hdr.biSizeImage = stride * h

    hdr_bytes = bytes(hdr)
    dib_data = hdr_bytes + bytes(pixels)

    hglob = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib_data))
    if not hglob:
        return {"ok": False, "error": "GlobalAlloc failed"}
    ptr = kernel32.GlobalLock(hglob)
    ctypes.memmove(ptr, dib_data, len(dib_data))
    kernel32.GlobalUnlock(hglob)

    deadline = time.time() + max(0.05, timeout_ms / 1000.0)
    while time.time() < deadline:
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                if not user32.SetClipboardData(CF_DIB, hglob):
                    return {"ok": False, "error": "SetClipboardData failed"}
            finally:
                user32.CloseClipboard()
            return {
                "ok": True, "format": "CF_DIB",
                "width": w, "height": h,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
            }
        time.sleep(0.025)
    kernel32.GlobalFree(hglob)
    return {"ok": False, "error": "clipboard_unavailable"}


# ---- Mouse -------------------------------------------------------------------

user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]


def mouse_position() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def mouse_move(x: int, y: int):
    user32.SetCursorPos(int(x), int(y))
    return {"ok": True, "x": int(x), "y": int(y)}


def mouse_click(x: int = None, y: int = None):
    if x is not None and y is not None:
        user32.SetCursorPos(int(x), int(y))
    time.sleep(0.01)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.01)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    pos = mouse_position()
    return {"ok": True, "clicked_at": pos}


# ---- Fast screenshot (mss) --------------------------------------------------

def _fast_screenshot():
    import mss
    from PIL import Image
    with mss.MSS() as sct:
        raw = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


# ---- Screen reader (WinRT OCR, GPU-accelerated) -----------------------------

_screen_reader = None


def _ensure_screen_reader():
    global _screen_reader
    if _screen_reader is None:
        import winocr
        _screen_reader = winocr
        print("[ScreenReader] WinRT OCR loaded (GPU-accelerated)")


def read_screen(spec: dict = None):
    spec = spec or {}
    _ensure_screen_reader()
    started = time.time()
    img = _fast_screenshot()
    result = _screen_reader.recognize_pil_sync(img, lang="en")
    elements = []
    for line in result.get("lines", []):
        for word in line.get("words", []):
            bb = word.get("bounding_rect", {})
            bx, by = bb.get("x", 0), bb.get("y", 0)
            bw, bh = bb.get("width", 0), bb.get("height", 0)
            cx = int(bx + bw / 2)
            cy = int(by + bh / 2)
            elements.append({
                "text": word.get("text", ""),
                "conf": 1.0,
                "center": [cx, cy],
                "bbox": [[int(bx), int(by)],
                         [int(bx + bw), int(by)],
                         [int(bx + bw), int(by + bh)],
                         [int(bx), int(by + bh)]],
            })
    return {
        "ok": True,
        "element_count": len(elements),
        "elements": elements,
        "elapsed_ms": round((time.time() - started) * 1000, 1),
    }


def ocr_screen(spec: dict = None):
    return read_screen(spec)


# ---- UIA element finder (fast, no screenshot) --------------------------------

def uia_find_element(target: str) -> list[dict]:
    if not _uia:
        return []
    target_lower = target.strip().lower()
    started = time.time()
    try:
        hwnd = user32.GetForegroundWindow()
        root = _uia.ElementFromHandle(hwnd)
        true_cond = _uia.CreateTrueCondition()
        all_elements = root.FindAll(UIA_TREE_SCOPE_DESCENDANTS, true_cond)
        matches = []
        count = all_elements.Length
        for idx in range(min(count, 500)):
            el = all_elements.GetElement(idx)
            try:
                name = el.CurrentName or ""
            except Exception:
                continue
            if not name or target_lower not in name.lower():
                continue
            try:
                rect = el.CurrentBoundingRectangle
                ct = el.CurrentControlType
            except Exception:
                continue
            l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
            if l == 0 and t == 0 and r == 0 and b == 0:
                continue
            matches.append({
                "name": name,
                "role": _uia_localized_names.get(ct, str(ct)),
                "rect": {"left": l, "top": t, "right": r, "bottom": b},
                "center": [int((l + r) / 2), int((t + b) / 2)],
            })
        return matches
    except Exception:
        return []


# ---- 8-way spatial direction -------------------------------------------------

import math

_last_locate = {"target": None, "center": None, "rect": None, "ts": 0, "found_via": None}


def spatial_direction(mouse_pos: tuple, target_center: tuple, target_rect: dict = None) -> dict:
    mx, my = mouse_pos
    tx, ty = target_center
    if target_rect:
        l, t, r, b = target_rect["left"], target_rect["top"], target_rect["right"], target_rect["bottom"]
        if l <= mx <= r and t <= my <= b:
            return {"direction": "on_target", "distance_px": 0, "angle_deg": 0}
    dx = tx - mx
    dy = my - ty
    dist = math.hypot(dx, dy)
    if dist < 3:
        return {"direction": "on_target", "distance_px": 0, "angle_deg": 0}
    angle = math.degrees(math.atan2(dy, dx)) % 360
    dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    idx = int((angle + 22.5) / 45) % 8
    return {"direction": dirs[idx], "distance_px": round(dist), "angle_deg": round(angle, 1)}


def _get_mouse_pos() -> tuple:
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def locate_target(spec: dict) -> dict:
    target = str(spec.get("target", ""))
    if not target:
        return {"ok": False, "error": "no target specified"}

    started = time.time()
    cache = _last_locate
    mouse = _get_mouse_pos()

    if (cache["target"] == target and cache["center"]
            and (time.time() - cache["ts"]) < 5.0):
        sd = spatial_direction(mouse, tuple(cache["center"]), cache["rect"])
        return {
            "ok": True, "target": target, "cached": True,
            "center": cache["center"], "found_via": cache["found_via"],
            **sd, "elapsed_ms": round((time.time() - started) * 1000, 1),
        }

    uia_matches = uia_find_element(target)
    if uia_matches:
        best = uia_matches[0]
        center = best["center"]
        rect = best["rect"]
        cache.update(target=target, center=center, rect=rect, ts=time.time(), found_via="uia")
        sd = spatial_direction(mouse, tuple(center), rect)
        return {
            "ok": True, "target": target, "cached": False,
            "matched_name": best["name"], "role": best["role"],
            "center": center, "found_via": "uia",
            **sd, "elapsed_ms": round((time.time() - started) * 1000, 1),
        }

    scan = read_screen()
    match = _best_text_match(target, scan.get("elements", []))
    if match:
        center = match["center"]
        cache.update(target=target, center=center, rect=None, ts=time.time(), found_via="screen_reader")
        sd = spatial_direction(mouse, tuple(center))
        return {
            "ok": True, "target": target, "cached": False,
            "matched_text": match["text"], "center": center,
            "found_via": "screen_reader",
            **sd, "elapsed_ms": round((time.time() - started) * 1000, 1),
        }

    return {
        "ok": False, "target": target,
        "error": f"'{target}' not found via UIA or screen reader",
        "elapsed_ms": round((time.time() - started) * 1000, 1),
    }


def _best_text_match(target: str, elements: list[dict]) -> dict | None:
    target_lower = target.strip().lower()
    best = None
    best_score = 0
    for el in elements:
        text = el.get("text", "").strip().lower()
        if target_lower == text:
            return el
        if target_lower in text:
            score = len(target_lower) / max(len(text), 1)
            if score > best_score:
                best_score = score
                best = el
        elif text in target_lower and len(text) > 2:
            score = len(text) / max(len(target_lower), 1) * 0.8
            if score > best_score:
                best_score = score
                best = el
    return best


def click_target(spec: dict):
    loc = locate_target(spec)
    if not loc.get("ok"):
        return loc
    cx, cy = loc["center"]
    mouse_click(cx, cy)
    return {
        "ok": True,
        "target": spec.get("target", ""),
        "matched_text": loc.get("matched_name") or loc.get("matched_text", ""),
        "clicked_at": [cx, cy],
        "found_via": loc.get("found_via"),
        "direction": loc.get("direction"),
        "distance_px": loc.get("distance_px"),
        "confidence": 1.0,
        "elapsed_ms": loc.get("elapsed_ms", 0),
    }


# ---- Gemini vision -----------------------------------------------------------

_GEMINI_PROMPT_TPL = (
    "Look at this screenshot. I need to: {goal}. "
    "Find the ONE element I should interact with. "
    "Reply: ELEMENT: [name] POSITION: [X%, Y%] "
    "where X%=from left edge, Y%=from top edge."
)

_GEMINI_ELEMENT_RE = re.compile(
    r'ELEMENT:\s*(.+?)\s*\n\s*POSITION:\s*'
    r'\[?\s*([\d.]+)\s*%?\s*,\s*([\d.]+)\s*%?\s*\]?',
    re.IGNORECASE | re.MULTILINE,
)

_GEMINI_ELEMENT_RE_INLINE = re.compile(
    r'ELEMENT:\s*(.+?)\s*POSITION:\s*'
    r'\[?\s*([\d.]+)\s*%?\s*,\s*([\d.]+)\s*%?\s*\]?',
    re.IGNORECASE,
)


def _poll_gemini_response(max_wait_s: float = 20.0,
                          poll_interval_s: float = 2.0,
                          stability_count: int = 2) -> dict:
    """Wait for Gemini to finish responding, then read the response via Ctrl+Shift+C."""
    time.sleep(4.0)

    deadline = time.time() + max_wait_s
    prev_text = ""
    stable = 0
    polls = 0

    while time.time() < deadline:
        hotkey("ctrl shift c")
        time.sleep(0.2)
        try:
            text = _read_clipboard_text(timeout_ms=500)
        except RuntimeError:
            text = ""
        polls += 1

        has_markers = ("element:" in text.lower() and "position:" in text.lower())

        if has_markers and text == prev_text:
            stable += 1
            if stable >= stability_count:
                return {"ok": True, "response_text": text, "polls": polls, "stable": True}
        else:
            stable = 0

        prev_text = text
        time.sleep(poll_interval_s)

    if prev_text:
        return {"ok": True, "response_text": prev_text, "polls": polls, "stable": False}
    return {"ok": False, "error": "gemini_response_timeout", "polls": polls}


def _parse_gemini_response(response_text: str,
                           screen_width: int = 1920,
                           screen_height: int = 1080) -> dict:
    """Parse ELEMENT/POSITION from Gemini's response into pixel coordinates."""
    matches = _GEMINI_ELEMENT_RE.findall(response_text)
    if not matches:
        matches = _GEMINI_ELEMENT_RE_INLINE.findall(response_text)
    if not matches:
        return {"ok": False, "error": "no_parseable_elements", "raw_text": response_text[:500]}

    elements = []
    for name, x_str, y_str in matches:
        x_pct = max(0.0, min(100.0, float(x_str)))
        y_pct = max(0.0, min(100.0, float(y_str)))
        elements.append({
            "name": name.strip(),
            "x_pct": round(x_pct, 1),
            "y_pct": round(y_pct, 1),
            "x": int(screen_width * x_pct / 100.0),
            "y": int(screen_height * y_pct / 100.0),
        })
    return {"ok": True, "elements": elements, "element_count": len(elements)}


def gemini_vision(spec: dict) -> dict:
    """Screenshot Ã¢â€ ' Gemini browser Ã¢â€ ' structured elements with pixel coordinates."""
    goal = str(spec.get("goal", ""))
    if not goal:
        return {"ok": False, "error": "no goal specified"}

    started = time.time()
    prompt = spec.get("prompt") or _GEMINI_PROMPT_TPL.format(goal=goal)
    max_wait_s = float(spec.get("wait_response_ms", 20000)) / 1000.0

    original = active_window()
    original_hwnd = original.get("hwnd")

    img = _fast_screenshot()
    screen_w, screen_h = img.size

    clip = _write_clipboard_image(img)
    if not clip.get("ok"):
        return {"ok": False, "error": "clipboard_image_write_failed", "detail": clip}

    hotkey("ctrl t")
    time.sleep(0.5)
    type_text("gemini.google.com")
    time.sleep(0.3)
    press("enter")
    time.sleep(3.0)

    hotkey("ctrl v")
    time.sleep(1.0)

    type_text(prompt)
    time.sleep(0.3)
    press("enter")

    poll = _poll_gemini_response(max_wait_s=max_wait_s)
    response_text = poll.get("response_text", "")

    parsed = _parse_gemini_response(response_text, screen_w, screen_h)

    if original_hwnd:
        focus_window(hwnd=original_hwnd)
        time.sleep(0.3)

    result = {
        "ok": parsed.get("ok", False),
        "elements": parsed.get("elements", []),
        "element_count": parsed.get("element_count", 0),
        "gemini_response": response_text[:1000],
        "original_hwnd": original_hwnd,
        "screen_size": [screen_w, screen_h],
        "elapsed_ms": round((time.time() - started) * 1000, 1),
    }
    if not parsed.get("ok"):
        result["error"] = parsed.get("error", "parse_failed")
    return result


def gemini_click(spec: dict) -> dict:
    """Vision-guided click: screenshot Ã¢â€ ' Gemini Ã¢â€ ' parse coordinates Ã¢â€ ' mouse click."""
    goal = str(spec.get("goal", ""))
    if not goal:
        return {"ok": False, "error": "no goal specified"}

    started = time.time()
    vision = gemini_vision(spec)

    if not vision.get("ok") or not vision.get("elements"):
        return {
            "ok": False,
            "error": vision.get("error", "vision_failed"),
            "gemini_response": vision.get("gemini_response", ""),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
        }

    chosen = vision["elements"][0]
    mouse_click(chosen["x"], chosen["y"])

    return {
        "ok": True,
        "goal": goal,
        "clicked_element": chosen["name"],
        "position_pct": [chosen["x_pct"], chosen["y_pct"]],
        "clicked_at": [chosen["x"], chosen["y"]],
        "screen_size": vision.get("screen_size"),
        "elements_found": vision.get("element_count", 0),
        "elapsed_ms": round((time.time() - started) * 1000, 1),
    }


# ---- Screen ------------------------------------------------------------------

def ensure_fullscreen(spec: dict = None):
    SW_MAXIMIZE = 3
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return {"ok": False, "error": "no foreground window"}
    is_maximized = bool(ctypes.windll.user32.IsZoomed(hwnd))
    if is_maximized:
        return {"ok": True, "was_maximized": True, "action": "none"}
    ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.15)
    now_maximized = bool(ctypes.windll.user32.IsZoomed(hwnd))
    return {"ok": now_maximized, "was_maximized": False, "action": "maximized"}


def capture_screen(spec: dict):
    started = time.time()
    out_dir = str(spec.get("out_dir") or os.path.join(os.path.dirname(__file__), "screenshots"))
    os.makedirs(out_dir, exist_ok=True)
    cache_key = str(spec.get("cache_key") or "screen")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in cache_key)[:80] or "screen"
    path = os.path.join(out_dir, f"{safe}-{int(time.time() * 1000)}.png")
    ps_path = path.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bmp)
try {{
  $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
  $bmp.Save('{ps_path}', [System.Drawing.Imaging.ImageFormat]::Png)
  [pscustomobject]@{{ok=$true; path='{ps_path}'; width=$bounds.Width; height=$bounds.Height; left=$bounds.Left; top=$bounds.Top}} | ConvertTo-Json -Compress
}} finally {{
  $graphics.Dispose()
  $bmp.Dispose()
}}
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        text=True,
        capture_output=True,
        timeout=float(spec.get("timeout_s", 8.0)),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "screen capture failed")[:500],
            "elapsed_ms": round((time.time() - started) * 1000, 2),
        }
    try:
        result = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "screen capture returned invalid JSON",
            "stdout": proc.stdout[:500],
            "elapsed_ms": round((time.time() - started) * 1000, 2),
        }
    result["elapsed_ms"] = round((time.time() - started) * 1000, 2)
    result["mime_type"] = "image/png"
    return result


# ---- Focus helpers for sweep -------------------------------------------------

def _focus_signature_from_state(state: dict) -> tuple:
    elem = state.get("focused_element") or {}
    active = state.get("active") or {}
    rect = elem.get("rect") or {}
    return (
        str(active.get("process") or ""),
        str(active.get("title") or ""),
        str(elem.get("control_type") or elem.get("localized_control_type") or ""),
        str(elem.get("name") or ""),
        str(elem.get("automation_id") or ""),
        str(elem.get("class_name") or ""),
        str(rect.get("left") or ""),
        str(rect.get("top") or ""),
        str(rect.get("right") or ""),
        str(rect.get("bottom") or ""),
    )


def _focus_record_from_state(state: dict, step_idx: int | None = None, tick_ms: int | None = None) -> dict:
    elem = state.get("focused_element") or {}
    active = state.get("active") or {}
    status_url = str(state.get("status_url", "") or elem.get("status_url", "") or "")
    record = {
        "index": step_idx,
        "name": str(elem.get("name", "") or ""),
        "control_type": str(elem.get("control_type", "") or elem.get("localized_control_type", "") or ""),
        "localized_control_type": str(elem.get("localized_control_type", "") or ""),
        "automation_id": str(elem.get("automation_id", "") or ""),
        "class_name": str(elem.get("class_name", "") or ""),
        "status_url": status_url,
        "active_title": active.get("title"),
        "active_process": active.get("process"),
        "has_keyboard_focus": elem.get("has_keyboard_focus"),
        "is_enabled": elem.get("is_enabled"),
        "is_offscreen": elem.get("is_offscreen"),
        "rect": elem.get("rect"),
        "signature": "|".join(_focus_signature_from_state(state)),
    }
    if tick_ms is not None:
        record["tick_ms"] = tick_ms
    return record


def _wait_focus_change(before_signature: tuple, tick_ms: float, poll_ms: float) -> dict:
    deadline = time.time() + tick_ms
    last_state = cached_focus_state()
    while time.time() < deadline:
        state = cached_focus_state()
        if _focus_signature_from_state(state) != before_signature:
            return state
        last_state = state
        time.sleep(poll_ms)
    return last_state


def _focus_record_matches(record: dict, stop_if: dict) -> bool:
    role = str(record.get("control_type") or record.get("localized_control_type") or "").lower()
    name = str(record.get("name") or "").lower()
    status_url = str(record.get("status_url") or "").lower()
    automation_id = str(record.get("automation_id") or "").lower()
    class_name = str(record.get("class_name") or "").lower()

    stop_role = str(stop_if.get("role", "")).lower()
    if stop_role and stop_role not in role:
        return False

    contains = [str(c).lower() for c in (
        stop_if.get("name_or_status_contains_any")
        or stop_if.get("name_contains_any")
        or []
    )]
    if contains and not any(c in name or c in status_url for c in contains):
        return False

    regexes = [str(c) for c in stop_if.get("name_regex_any", [])]
    if regexes and not any(
        re.search(pattern, name, flags=re.IGNORECASE)
        or re.search(pattern, status_url, flags=re.IGNORECASE)
        for pattern in regexes
    ):
        return False

    reject_regexes = [str(c) for c in stop_if.get("name_regex_not_any", [])]
    if reject_regexes and any(
        re.search(pattern, name, flags=re.IGNORECASE)
        or re.search(pattern, status_url, flags=re.IGNORECASE)
        for pattern in reject_regexes
    ):
        return False

    automation_contains = [str(c).lower() for c in stop_if.get("automation_id_contains_any", [])]
    if automation_contains and not any(c in automation_id for c in automation_contains):
        return False

    class_contains = [str(c).lower() for c in stop_if.get("class_name_contains_any", [])]
    if class_contains and not any(c in class_name for c in class_contains):
        return False

    return True


def _save_tab_sweep(cache_key: str, payload: dict) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in cache_key)[:120]
    if not safe:
        safe = f"sweep-{int(time.time())}"
    out_dir = os.path.join(os.path.dirname(__file__), "tab_sweeps")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{safe}.json")
    tmp = f"{path}.tmp-{os.getpid()}-{time.time_ns()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ---- Sweep -------------------------------------------------------------------

def arrow_sweep(spec: dict):
    key = spec.get("key", "down")
    max_steps = int(spec.get("max_steps", 50))
    min_steps = int(spec.get("min_steps", 2))
    tick_ms = float(spec.get("tick_ms", TIMING["tick_ms"] + 50)) / 1000.0
    key_hold_ms = float(spec.get("key_hold_ms", TIMING["key_hold_ms"]))
    poll_ms = float(spec.get("poll_ms", TIMING["poll_ms"])) / 1000.0
    expand_first = bool(spec.get("expand_first", False))
    collapse_after = bool(spec.get("collapse_after", False))
    stop_on_cycle = bool(spec.get("stop_on_cycle", True))
    stuck_limit = int(spec.get("stuck_limit", 2))
    return_limit = int(spec.get("return_limit", 100))

    initial_state = cached_focus_state(max_age_ms=0)
    parent = _focus_record_from_state(initial_state, 0, 0)

    if expand_first:
        tap("space", hold_ms=key_hold_ms)
        time.sleep(tick_ms)

    def _cycle_key(record):
        return (record.get("name", ""), record.get("control_type", ""), record.get("automation_id", ""))

    elements = []
    tick_times_ms = []
    first_cycle_key = None
    prev_cycle_key = None
    stuck_count = 0
    cycle = None

    for step_idx in range(max_steps):
        t0 = time.time()
        before_signature = _focus_signature_from_state(cached_focus_state())
        tap(key, hold_ms=key_hold_ms)
        state = _wait_focus_change(before_signature, tick_ms, poll_ms)
        elapsed = int((time.time() - t0) * 1000)
        tick_times_ms.append(elapsed)

        record = _focus_record_from_state(state, step_idx + 1, elapsed)
        ck = _cycle_key(record)

        if first_cycle_key is None:
            first_cycle_key = ck
            elements.append(record)
        elif stop_on_cycle and step_idx + 1 >= min_steps and ck == first_cycle_key:
            cycle = {"from_index": 1, "to_index": step_idx + 1, "length": len(elements)}
            break
        else:
            if ck == prev_cycle_key:
                stuck_count += 1
            else:
                stuck_count = 0
            if stuck_count >= stuck_limit:
                break
            elements.append(record)

        prev_cycle_key = ck

    if collapse_after:
        tap("escape", hold_ms=key_hold_ms)
        time.sleep(0.05)

    payload = {
        "ok": True,
        "arrow_sweep_found": True,
        "key": key,
        "parent": parent,
        "steps_executed": len(tick_times_ms),
        "element_count": len(elements),
        "avg_tick_ms": sum(tick_times_ms) / len(tick_times_ms) if tick_times_ms else 0,
        "cycle": cycle,
        "stuck": stuck_count >= stuck_limit,
        "elements": elements[:return_limit],
        "truncated": len(elements) > return_limit,
    }

    if bool(spec.get("compact", False)):
        payload["elements"] = [
            {"index": el.get("index"), "name": el.get("name", ""),
             "role": (el.get("control_type") or el.get("localized_control_type") or "").replace("ControlType.", "")}
            for el in payload["elements"]
        ]

    return payload


def tab_sweep(spec: dict):
    key = spec.get("key", "tab")
    max_steps = int(spec.get("max_steps", 160))
    min_steps = int(spec.get("min_steps", 20))
    tick_ms = float(spec.get("tick_ms", TIMING["tick_ms"])) / 1000.0
    key_hold_ms = float(spec.get("key_hold_ms", TIMING["key_hold_ms"]))
    poll_ms = float(spec.get("poll_ms", TIMING["poll_ms"])) / 1000.0
    stop_on_cycle = bool(spec.get("stop_on_cycle", True))
    include_empty = bool(spec.get("include_empty", False))
    return_limit = int(spec.get("return_limit", 220))
    stop_if = spec.get("stop_if", {}) or {}
    cache_key = str(spec.get("cache_key") or "")
    activate_on_match = bool(spec.get("activate_on_match", False))
    activation_key = str(spec.get("activation_key", "enter"))
    activation_settle_ms = float(spec.get("activation_settle_ms", 0))
    check_initial = bool(spec.get("check_initial", False))
    search_lists = bool(spec.get("search_lists", bool(stop_if)))

    elements = []
    trace = []
    seen = {}
    tick_times_ms = []
    cycle = None
    match = None

    if check_initial:
        state = cached_focus_state(max_age_ms=0)
        record = _focus_record_from_state(state, 0, 0)
        signature = record["signature"]
        has_focus_data = any(
            record.get(field)
            for field in ("name", "control_type", "automation_id", "class_name", "status_url")
        )
        if include_empty or has_focus_data:
            elements.append(record)
            if len(trace) < 16:
                trace.append(record)
        if stop_if and _focus_record_matches(record, stop_if):
            match = record
            if activate_on_match:
                activation = press(activation_key)
                if activation_settle_ms > 0:
                    time.sleep(activation_settle_ms / 1000.0)
                record["activation"] = activation
            tick_times_ms.append(0)
        seen.setdefault(signature, 0)

    for step_idx in range(max_steps if match is None else 0):
        t0 = time.time()
        before_signature = _focus_signature_from_state(cached_focus_state())
        tap(key, hold_ms=key_hold_ms)
        state = _wait_focus_change(before_signature, tick_ms, poll_ms)
        elapsed = int((time.time() - t0) * 1000)
        tick_times_ms.append(elapsed)
        record = _focus_record_from_state(state, step_idx + 1, elapsed)
        signature = record["signature"]

        has_focus_data = any(
            record.get(field)
            for field in ("name", "control_type", "automation_id", "class_name", "status_url")
        )
        if include_empty or has_focus_data:
            elements.append(record)
            if len(trace) < 16:
                trace.append(record)

        if stop_if and _focus_record_matches(record, stop_if):
            match = record
            if activate_on_match:
                activation = press(activation_key)
                if activation_settle_ms > 0:
                    time.sleep(activation_settle_ms / 1000.0)
                record["activation"] = activation
            break

        if stop_on_cycle and step_idx + 1 >= min_steps and signature in seen:
            cycle = {
                "from_index": seen[signature],
                "to_index": step_idx + 1,
                "length": step_idx + 1 - seen[signature],
                "signature": signature,
            }
            break
        seen.setdefault(signature, step_idx + 1)

    list_search_result = None
    if not match and stop_if and search_lists:
        list_items = [el for el in elements if "ListItem" in (el.get("control_type") or "")]
        seen_list_names = set()
        for li in list_items:
            li_name = li.get("name", "")
            if not li_name or li_name in seen_list_names:
                continue
            seen_list_names.add(li_name)
            focus_document({})
            time.sleep(0.15)
            nav_back = tab_sweep({
                "key": "tab", "max_steps": max_steps, "stop_on_cycle": True,
                "stop_if": {"name_or_status_contains_any": [li_name]},
                "search_lists": False,
            })
            if not nav_back.get("match"):
                continue
            arrow_result = arrow_sweep({
                "key": "down", "max_steps": 30, "stop_on_cycle": True,
                "tick_ms": 100, "min_steps": 2,
            })
            for sibling in arrow_result.get("elements", []):
                if _focus_record_matches(sibling, stop_if):
                    match = sibling
                    list_search_result = {
                        "found_in_list": li_name,
                        "sibling_index": sibling.get("index"),
                        "siblings_checked": len(arrow_result.get("elements", [])),
                    }
                    if activate_on_match:
                        activation = press(activation_key)
                        if activation_settle_ms > 0:
                            time.sleep(activation_settle_ms / 1000.0)
                        match["activation"] = activation
                    break
            if match:
                break

    payload = {
        "ok": True,
        "tab_sweep_found": True,
        "key": key,
        "steps_executed": len(tick_times_ms),
        "element_count": len(elements),
        "avg_tick_ms": sum(tick_times_ms) / len(tick_times_ms) if tick_times_ms else 0,
        "cycle": cycle,
        "match": match,
        "activated": bool(match and activate_on_match),
        "activation_key": activation_key if match and activate_on_match else None,
        "elements": elements[:return_limit],
        "truncated": len(elements) > return_limit,
        "trace_sample": trace,
        "list_search": list_search_result,
    }
    if bool(spec.get("compact", False)):
        payload["elements"] = [
            {"index": el.get("index"), "name": el.get("name", ""),
             "role": (el.get("control_type") or el.get("localized_control_type") or "").replace("ControlType.", "")}
            for el in payload["elements"]
        ]
        if payload.get("match"):
            m = payload["match"]
            payload["match"] = {"index": m.get("index"), "name": m.get("name", ""),
                "role": (m.get("control_type") or m.get("localized_control_type") or "").replace("ControlType.", ""),
                "activation": m.get("activation")}
        payload["trace_sample"] = None

    if cache_key:
        payload["cache_path"] = _save_tab_sweep(cache_key, payload)
    return payload


def focus_document(spec: dict):
    max_steps = int(spec.get("max_steps", 5))
    key = str(spec.get("key", "f6"))
    tick_ms = float(spec.get("tick_ms", TIMING["tick_ms"])) / 1000.0
    key_hold_ms = float(spec.get("key_hold_ms", TIMING["key_hold_ms"]))
    stop_if = spec.get("stop_if") or {
        "role": "Document",
        "name_regex_any": [".+"],
    }
    trace = []

    for step_idx in range(max_steps + 1):
        state = observe_state(include_windows=False, include_uia=True)
        record = _focus_record_from_state(state, step_idx, None)
        trace.append(record)
        if _focus_record_matches(record, stop_if):
            return {
                "ok": True,
                "focus_document_found": True,
                "steps_executed": step_idx,
                "match": record,
                "trace_sample": trace,
            }
        if step_idx >= max_steps:
            break
        press(key, hold_ms=key_hold_ms)
        time.sleep(tick_ms)

    return {
        "ok": False,
        "error": "document focus not reached",
        "steps_executed": max_steps,
        "last_checked": trace[-1] if trace else {},
        "trace_sample": trace,
    }


# ---- Route dispatcher --------------------------------------------------------

def run_route(steps: list[dict]):
    results = []
    route_started = time.time()
    for i, step in enumerate(steps):
        step_started = time.time()

        def append(payload: dict) -> None:
            payload.setdefault("elapsed_ms", round((time.time() - step_started) * 1000, 2))
            results.append(payload)

        if "wait_until" in step:
            spec = step["wait_until"] or {}
            timeout_ms = float(spec.get("timeout_ms", 3000))
            poll_ms = float(spec.get("poll_ms", 50))
            target_process = (spec.get("process") or "").lower()
            target_title = (spec.get("title_contains") or "").lower()
            target_focus_name = (spec.get("focus_name_contains") or "").lower()

            deadline = time.time() + (timeout_ms / 1000.0)
            matched = False
            last_state = {}
            while time.time() < deadline:
                state = observe_state(include_windows=False, include_uia=bool(target_focus_name))
                active = state.get("active") or {}
                focused = state.get("focused_element") or {}
                last_state = {"process": active.get("process"), "title": active.get("title"), "focus_name": focused.get("name")}

                proc_ok = (not target_process) or (target_process in (active.get("process") or "").lower())
                title_ok = (not target_title) or (target_title in (active.get("title") or "").lower())
                focus_ok = (not target_focus_name) or (target_focus_name in (focused.get("name") or "").lower())

                if proc_ok and title_ok and focus_ok:
                    matched = True
                    break
                time.sleep(poll_ms / 1000.0)

            if matched:
                append({"step": i, "ok": True, "wait_until_matched": True, "state": last_state})
            else:
                return {"ok": False, "error": "wait_until timed out", "step": i, "spec": spec, "last_state": last_state, "steps": results}
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
            append({"step": i, "ok": True, "slept": step["sleep"]})
        elif "hotkey" in step:
            r = hotkey(step["hotkey"])
            append({"step": i, **r})
        elif "type" in step:
            r = type_text(step["type"])
            append({"step": i, **r})
        elif "press_if_focus" in step:
            spec = step["press_if_focus"] or {}
            state = cached_focus_state(max_age_ms=int(spec.get("max_age_ms", 1500)))
            ok = state.get("ok") and not state.get("stale") and focus_matches(
                state,
                name_contains=spec.get("name_contains"),
                title_contains=spec.get("title_contains"),
                process_contains=spec.get("process_contains"),
                control_type_contains=spec.get("control_type_contains"),
            )
            if not ok:
                return {"ok": False, "error": "focus guard failed", "step": i, "guard": spec, "steps": results}
            r = press(spec.get("key", "enter"))
            append({"step": i, "guarded": True, **r})
        elif "press" in step:
            r = press(step["press"])
            append({"step": i, **r})
        elif "capture_screen" in step:
            r = capture_screen(step["capture_screen"] or {})
            if not r.get("ok"):
                return {"ok": False, "error": "capture_screen failed", "step": i, "result": r, "steps": results}
            append({"step": i, "screen_captured": True, **r})
        elif "snapshot" in step:
            spec = step["snapshot"] or {}
            screen = capture_screen(spec)
            state = observe_state(include_windows=True, include_uia=True)
            active = state.get("active") or {}
            url_hint = ""
            t = active.get("title") or ""
            for sep in (" -- ", " - ", " | "):
                if sep in t:
                    parts = t.split(sep)
                    url_hint = parts[-1].strip()
                    break
            domain = ""
            for part in t.replace(" ", "").split("/"):
                if "." in part and len(part) > 3:
                    domain = part.split("/")[0].lower()
                    break
            payload = {
                "step": i, "ok": screen.get("ok", False),
                "screenshot": screen.get("path", ""),
                "active_window": active,
                "focused_element": state.get("focused_element") or {},
                "window_title": t,
                "url_hint": url_hint,
                "domain_hint": domain,
                "mime_type": "image/png",
            }
            if state.get("windows"):
                payload["all_windows"] = state["windows"]
            append(payload)
        elif "ensure_fullscreen" in step:
            r = ensure_fullscreen(step["ensure_fullscreen"] or {})
            append({"step": i, **r})
        elif "arrow_sweep" in step:
            r = arrow_sweep(step["arrow_sweep"] or {})
            append({"step": i, **r})
        elif "tab_sweep" in step:
            r = tab_sweep(step["tab_sweep"] or {})
            append({"step": i, **r})
        elif "focus_document" in step:
            r = focus_document(step["focus_document"] or {})
            if not r.get("ok"):
                return {"ok": False, "error": "focus_document failed", "step": i, "result": r, "steps": results}
            append({"step": i, **r})
        elif "focus" in step:
            f = step["focus"] or {}
            r = focus_window(title_contains=f.get("title_contains"), hwnd=f.get("hwnd"))
            append({"step": i, **r})
        elif "observe" in step:
            o = step["observe"] or {}
            r = observe_state(include_windows=bool(o.get("windows", False)), include_uia=bool(o.get("uia", True)))
            append({"step": i, "ok": True, "observe": r})
        elif "mouse_move" in step:
            spec = step["mouse_move"] or {}
            r = mouse_move(int(spec.get("x", 0)), int(spec.get("y", 0)))
            append({"step": i, **r})
        elif "mouse_click" in step:
            spec = step["mouse_click"] or {}
            r = mouse_click(spec.get("x"), spec.get("y"))
            append({"step": i, **r})
        elif "ocr_scan" in step:
            r = read_screen(step["ocr_scan"] or {})
            append({"step": i, **r})
        elif "locate_target" in step:
            r = locate_target(step["locate_target"] or {})
            append({"step": i, **r})
        elif "click_target" in step:
            r = click_target(step["click_target"] or {})
            append({"step": i, **r})
        elif "vision_query" in step:
            r = gemini_vision(step["vision_query"] or {})
            append({"step": i, **r})
        elif "gemini_click" in step:
            r = gemini_click(step["gemini_click"] or {})
            append({"step": i, **r})
        else:
            raise ValueError(f"Unknown route step at {i}: {step}")

    return {
        "ok": True,
        "route_elapsed_ms": round((time.time() - route_started) * 1000, 2),
        "step_count": len(results),
        "steps": results,
    }


# ---- Command file watcher ----------------------------------------------------

class CommandFileWatcher(threading.Thread):
    def __init__(self, path: str, allow_input: bool, interval: float = 0.05):
        super().__init__(daemon=True)
        self.path = path
        self.result_path = os.path.splitext(path)[0] + ".result.json"
        self.allow_input = allow_input
        self.interval = interval
        self.last_id = self._initial_command_id()

    def _initial_command_id(self):
        try:
            raw = Path(self.path).read_text(encoding="utf-8-sig").strip()
            if raw:
                return json.loads(raw).get("id")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return None

    def write_result(self, payload):
        tmp = (
            f"{self.result_path}.tmp-{os.getpid()}-"
            f"{threading.get_ident()}-{time.time_ns()}"
        )
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        last_error = None
        for attempt in range(20):
            try:
                os.replace(tmp, self.result_path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(min(0.05, 0.005 * (attempt + 1)))
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:
                    raise
                last_error = exc
                time.sleep(min(0.05, 0.005 * (attempt + 1)))
        try:
            os.remove(tmp)
        except OSError:
            pass
        if last_error is not None:
            raise last_error
        raise RuntimeError("failed to write command result")

    def _handle_command(self, cmd: dict) -> None:
        cmd_id = cmd.get("id")
        if not cmd_id or cmd_id == self.last_id:
            return
        if cmd.get("observe"):
            o = cmd.get("observe") or {}
            if o.get("cached"):
                result = cached_focus_state(max_age_ms=o.get("max_age_ms"))
                if o.get("windows"):
                    result["windows"] = list_windows()
            else:
                result = observe_state(
                    include_windows=bool(o.get("windows", False)),
                    include_uia=bool(o.get("uia", True)),
                )
        elif not self.allow_input:
            result = {"ok": False, "id": cmd_id, "error": "input disabled"}
        else:
            result = run_route(cmd.get("steps", []))
        result["id"] = cmd_id
        self.write_result(result)
        self.last_id = cmd_id

    def run(self):
        while True:
            cmd_id = None
            try:
                if os.path.exists(self.path):
                    with open(self.path, "r", encoding="utf-8-sig") as f:
                        raw = f.read().strip()
                    if raw:
                        cmd = json.loads(raw)
                        cmd_id = cmd.get("id")
                        if cmd_id and cmd_id != self.last_id:
                            self._handle_command(cmd)
            except Exception as e:
                self.write_result({"ok": False, "id": cmd_id, "error": str(e)})
            time.sleep(self.interval)

# ---- HTTP --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    allow_input = False

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        try:
            if self.path == "/health":
                self._json(200, {"ok": True, "service": "Yaldabaoth", "allow_input": self.allow_input})
            elif self.path == "/windows":
                self._json(200, {"ok": True, "windows": list_windows()})
            elif self.path == "/active":
                self._json(200, {"ok": True, "active": active_window()})
            elif self.path == "/focus-state":
                self._json(200, observe_state(include_uia=True))
            elif self.path == "/focus-live":
                self._json(200, cached_focus_state(max_age_ms=500))
            elif self.path == "/gui-focus":
                self._json(200, gui_thread_focus())
            elif self.path == "/uia-focus":
                self._json(200, uia_focused_element())
            else:
                self._json(404, {"ok": False, "error": "unknown endpoint"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _check_target_guard(self, data):
        guard = data.get("target_title_contains") or data.get("active_title_contains")
        if not guard:
            return None
        active = active_window()
        if not active or guard.lower() not in active.get("title", "").lower():
            return {"ok": False, "error": "target guard failed", "wanted_title_contains": guard, "active": active}
        return None

    def do_POST(self):
        try:
            data = self._body()
            if self.path == "/focus":
                self._json(200, focus_window(title_contains=data.get("title_contains"), hwnd=data.get("hwnd")))
                return
            if not self.allow_input:
                self._json(403, {"ok": False, "error": "input disabled; restart with --allow-input"})
                return
            guard_failure = self._check_target_guard(data)
            if guard_failure:
                self._json(409, guard_failure)
                return
            if self.path == "/press":
                result = press(data["key"])
                result["active_after"] = active_window()
                self._json(200, result)
            elif self.path == "/hotkey":
                result = hotkey(data["keys"])
                result["active_after"] = active_window()
                self._json(200, result)
            elif self.path == "/type":
                result = type_text(data["text"])
                result["active_after"] = active_window()
                self._json(200, result)
            elif self.path == "/route":
                self._json(200, run_route(data["steps"]))
            else:
                self._json(404, {"ok": False, "error": "unknown endpoint"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


def main():
    default_command_path = os.environ.get(
        "VK_COMMAND_PATH",
        str(Path(__file__).resolve().parent / "command.json"),
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-input", action="store_true", help="Enable keyboard input endpoints")
    parser.add_argument("--command-path", default=default_command_path,
                        help="Path to the command.json file watched for routes (default: alongside this script, or $VK_COMMAND_PATH)")
    args = parser.parse_args()
    Handler.allow_input = args.allow_input
    command_path = args.command_path
    CommandFileWatcher(command_path, allow_input=args.allow_input).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Yaldabaoth listening on http://{args.host}:{args.port} allow_input={args.allow_input} command_file={command_path}")
    server.serve_forever()

if __name__ == "__main__":
    main()
