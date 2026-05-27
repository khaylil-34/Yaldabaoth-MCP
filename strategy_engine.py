"""Strategy knowledge base for Yaldabaoth auto-research.

Manages strategies.json -- tracks which desktop navigation approaches work,
which fail, and surfaces the best known approach for any task.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from yald_bridge import atomic_write

EMPTY_DB: dict[str, Any] = {
    "version": 2,
    "strategies": {},
    "site_knowledge": {},
    "anti_patterns": [],
    "tool_knowledge": {},
}


_TOOL_RE = re.compile(r'\bvk_\w+', re.IGNORECASE)


def _extract_tool_names(approach: dict) -> list[str]:
    text = approach.get("id", "")
    for step in approach.get("steps", []):
        if isinstance(step, dict):
            text += " " + json.dumps(step)
    return list(set(_TOOL_RE.findall(text)))


def update_tool_knowledge(data: dict, tool_name: str,
                          success: bool, duration_ms: float,
                          task_context: str = "") -> None:
    tools = data.setdefault("tool_knowledge", {})
    entry = tools.setdefault(tool_name, {
        "avg_duration_ms": 0, "total_attempts": 0,
        "total_successes": 0, "success_rate": 0,
        "best_for": [], "notes": [],
    })
    entry["total_attempts"] = entry.get("total_attempts", 0) + 1
    if success:
        entry["total_successes"] = entry.get("total_successes", 0) + 1
    n = entry["total_attempts"]
    entry["success_rate"] = round(entry["total_successes"] / n, 3)
    if duration_ms > 0:
        prev = entry.get("avg_duration_ms", 0)
        entry["avg_duration_ms"] = round(prev + (duration_ms - prev) / n, 1)
    if task_context and success:
        bf = entry.setdefault("best_for", [])
        if task_context not in bf:
            bf.append(task_context)
            if len(bf) > 10:
                bf[:] = bf[-10:]
    entry["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_tool_preferences(data: dict,
                         keywords: list[str] | None = None) -> list[dict]:
    tools = data.get("tool_knowledge", {})
    if not tools:
        return []
    entries = []
    for name, info in tools.items():
        score = info.get("success_rate", 0)
        if keywords:
            bf = [b.lower() for b in info.get("best_for", [])]
            if any(k.lower() in " ".join(bf) for k in keywords):
                score += 0.5
        entries.append({"tool": name, "relevance": round(score, 2), **info})
    entries.sort(key=lambda e: (-e["relevance"], e.get("avg_duration_ms", 9999)))
    return entries[:5]


def _migrate_v1_to_v2(data: dict) -> dict:
    if data.get("version", 1) >= 2:
        return data
    data.setdefault("tool_knowledge", {})
    for skey, strategy in data.get("strategies", {}).items():
        for approach in strategy.get("approaches", []):
            tools = _extract_tool_names(approach)
            for tool_name in tools:
                update_tool_knowledge(
                    data, tool_name,
                    approach.get("success_rate", 0) > 0.5,
                    approach.get("avg_duration_ms", 0),
                    skey,
                )
    data["version"] = 2
    return data


def load_strategies(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return json.loads(json.dumps(EMPTY_DB))
    data = json.loads(path.read_text(encoding="utf-8"))
    return _migrate_v1_to_v2(data)


def save_strategies(path: str | Path, data: dict[str, Any]) -> None:
    atomic_write(Path(path), json.dumps(data, indent=2, ensure_ascii=False))


def _normalize_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_strategy(data: dict[str, Any], task: str,
                  tags: list[str] | None = None) -> dict[str, Any] | None:
    """Fuzzy-match a task description to a known strategy."""
    strategies = data.get("strategies", {})
    if not strategies:
        return None

    key = _normalize_key(task)
    if key in strategies:
        return strategies[key]

    best_score = 0.0
    best_match: dict[str, Any] | None = None

    for skey, strategy in strategies.items():
        score = _similarity(task, strategy.get("description", skey))
        if tags:
            stags = set(strategy.get("tags", []))
            tag_overlap = len(set(tags) & stags) / max(len(tags), 1)
            score = score * 0.7 + tag_overlap * 0.3
        if score > best_score:
            best_score = score
            best_match = strategy

    if best_score >= 0.4:
        return best_match
    return None


def get_best_approach(strategy: dict[str, Any]) -> dict[str, Any] | None:
    """Return the approach with the highest success rate (min 1 attempt)."""
    approaches = strategy.get("approaches", [])
    if not approaches:
        return None

    best_id = strategy.get("best_approach")
    if best_id:
        for a in approaches:
            if a["id"] == best_id:
                return a

    ranked = sorted(approaches,
                    key=lambda a: (a.get("success_rate", 0), a.get("attempts", 0)),
                    reverse=True)
    return ranked[0] if ranked else None


def record_attempt(data: dict[str, Any], strategy_key: str, approach_id: str,
                   success: bool, duration_ms: float = 0,
                   failure_reason: str = "") -> None:
    """Record a single attempt result for an approach."""
    strategies = data.setdefault("strategies", {})
    strategy = strategies.get(strategy_key)
    if not strategy:
        return

    for approach in strategy.get("approaches", []):
        if approach["id"] == approach_id:
            approach["attempts"] = approach.get("attempts", 0) + 1
            if success:
                approach["successes"] = approach.get("successes", 0) + 1
            approach["success_rate"] = (
                approach["successes"] / approach["attempts"]
                if approach["attempts"] > 0 else 0
            )
            if duration_ms > 0:
                prev_avg = approach.get("avg_duration_ms", 0)
                n = approach["attempts"]
                approach["avg_duration_ms"] = round(
                    prev_avg + (duration_ms - prev_avg) / n, 1
                )
            approach["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if failure_reason and not success:
                modes = approach.setdefault("failure_modes", [])
                if failure_reason not in modes:
                    modes.append(failure_reason)
            tool_names = _extract_tool_names(approach)
            for tn in tool_names:
                update_tool_knowledge(data, tn, success, duration_ms, strategy_key)
            break

    _update_best(strategy)


def _update_best(strategy: dict[str, Any]) -> None:
    best = get_best_approach(strategy)
    if best:
        strategy["best_approach"] = best["id"]


def add_approach(data: dict[str, Any], strategy_key: str,
                 approach: dict[str, Any]) -> None:
    """Register a new approach for a strategy."""
    strategies = data.setdefault("strategies", {})
    strategy = strategies.get(strategy_key)
    if not strategy:
        return

    approach.setdefault("attempts", 0)
    approach.setdefault("successes", 0)
    approach.setdefault("success_rate", 0)
    approach.setdefault("avg_duration_ms", 0)
    approach.setdefault("failure_modes", [])

    existing_ids = {a["id"] for a in strategy.get("approaches", [])}
    if approach["id"] not in existing_ids:
        strategy.setdefault("approaches", []).append(approach)
        _update_best(strategy)


def create_strategy(data: dict[str, Any], key: str, description: str,
                    tags: list[str] | None = None) -> dict[str, Any]:
    """Create a new empty strategy entry."""
    strategies = data.setdefault("strategies", {})
    if key not in strategies:
        strategies[key] = {
            "description": description,
            "approaches": [],
            "tags": tags or [],
        }
    return strategies[key]


def add_anti_pattern(data: dict[str, Any], description: str,
                     context: str = "") -> None:
    """Record something to avoid."""
    patterns = data.setdefault("anti_patterns", [])
    for existing in patterns:
        if existing.get("description") == description:
            return
    patterns.append({
        "description": description,
        "context": context,
        "discovered": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def get_anti_patterns(data: dict[str, Any],
                      keywords: list[str] | None = None) -> list[dict[str, Any]]:
    """Get anti-patterns, optionally filtered by keywords."""
    patterns = data.get("anti_patterns", [])
    if not keywords:
        return patterns
    kw_lower = [k.lower() for k in keywords]
    return [
        p for p in patterns
        if any(k in p.get("description", "").lower() or k in p.get("context", "").lower()
               for k in kw_lower)
    ]


def get_site_knowledge(data: dict[str, Any], domain: str) -> dict[str, Any]:
    """Retrieve knowledge about a specific site/domain."""
    return data.get("site_knowledge", {}).get(domain, {})


def update_site_knowledge(data: dict[str, Any], domain: str,
                          key: str, value: Any) -> None:
    """Update a piece of site-specific knowledge."""
    sites = data.setdefault("site_knowledge", {})
    site = sites.setdefault(domain, {})
    site[key] = value


def prune_strategies(data: dict[str, Any], min_attempts: int = 10,
                     max_failure_rate: float = 0.8) -> list[str]:
    """Remove approaches that consistently fail. Returns list of pruned IDs."""
    pruned = []
    for strategy in data.get("strategies", {}).values():
        approaches = strategy.get("approaches", [])
        survivors = []
        for a in approaches:
            if (a.get("attempts", 0) >= min_attempts
                    and a.get("success_rate", 1) < (1 - max_failure_rate)):
                pruned.append(a["id"])
            else:
                survivors.append(a)
        if len(survivors) < len(approaches):
            strategy["approaches"] = survivors
            _update_best(strategy)
    return pruned


def export_summary(data: dict[str, Any]) -> str:
    """Generate a compact text summary of all strategies for AI consumption."""
    lines = ["# Known Desktop Strategies\n"]
    for key, strategy in data.get("strategies", {}).items():
        best = get_best_approach(strategy)
        if best:
            rate = f"{best.get('success_rate', 0):.0%}"
            lines.append(f"- **{strategy.get('description', key)}**: "
                         f"best={best['id']} ({rate} over {best.get('attempts', 0)} attempts)")
        else:
            lines.append(f"- **{strategy.get('description', key)}**: no proven approach yet")

    anti = data.get("anti_patterns", [])
    if anti:
        lines.append("\n## Anti-patterns\n")
        for p in anti:
            lines.append(f"- {p['description']}")

    return "\n".join(lines)
