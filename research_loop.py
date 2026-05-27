#!/usr/bin/env python3
"""Yaldabaoth AutoResearch Loop -- autonomous desktop strategy optimization.

Adapted from Karpathy's autoresearch pattern. Instead of optimizing ML metrics,
this ratchet loop optimizes desktop navigation strategies by running experiments,
measuring success/failure, and committing improvements to strategies.json.

Usage:
    python research_loop.py                      # run with defaults
    python research_loop.py --program program.md # custom program file
    python research_loop.py --once               # single experiment, then exit
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from yald_bridge import YaldBridge
from strategy_engine import (
    load_strategies, save_strategies, find_strategy, get_best_approach,
    record_attempt, add_approach, create_strategy, add_anti_pattern,
    prune_strategies, export_summary,
)

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_COMMAND_PATH = Path(os.environ.get(
    "VK_COMMAND_PATH", str(SCRIPT_DIR / "command.json")))
DEFAULT_RESULT_PATH = Path(os.environ.get(
    "VK_RESULT_PATH", str(SCRIPT_DIR / "command.result.json")))
DEFAULT_STRATEGIES = SCRIPT_DIR / "strategies.json"
DEFAULT_PROGRAM = SCRIPT_DIR / "program.md"
DEFAULT_LOG = SCRIPT_DIR / "experiment_log.jsonl"


@dataclass
class Task:
    key: str
    description: str
    steps: list[dict[str, Any]]
    verification: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass
class ExperimentResult:
    task_key: str
    approach_id: str
    success: bool
    duration_ms: float
    error: str = ""
    verification_detail: str = ""
    committed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_program(path: Path) -> dict[str, Any]:
    """Parse program.md into structured research tasks."""
    if not path.exists():
        return {"goal": "No program file found", "tasks": [], "rules": []}

    text = path.read_text(encoding="utf-8")
    result: dict[str, Any] = {"goal": "", "tasks": [], "rules": [], "raw": text}

    current_section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped[3:].strip().lower()
        elif stripped.startswith("- ") and current_section:
            item = stripped[2:].strip()
            if "task" in current_section or "goal" in current_section:
                result["tasks"].append(item)
            elif "rule" in current_section:
                result["rules"].append(item)
            elif "verification" in current_section:
                result.setdefault("verification_methods", []).append(item)
        elif stripped and current_section == "current goal":
            result["goal"] = stripped

    return result


class ResearchLoop:
    def __init__(self, bridge: YaldBridge, strategies_path: Path,
                 program_path: Path, log_path: Path):
        self.bridge = bridge
        self.strategies_path = strategies_path
        self.program_path = program_path
        self.log_path = log_path
        self.strategies = load_strategies(strategies_path)
        self.program = parse_program(program_path)
        self.experiment_count = 0

    def run_experiment(self, task: Task) -> ExperimentResult:
        """Run a single experiment: execute steps, verify, record result."""
        self.experiment_count += 1

        strategy = find_strategy(self.strategies, task.description, task.tags)
        approach_id = task.key.replace(" ", "-")

        if strategy:
            best = get_best_approach(strategy)
            if best and best.get("success_rate", 0) > 0.8:
                approach_id = best["id"]
                steps = best.get("steps", task.steps)
            else:
                steps = task.steps
        else:
            strategy_key = task.key.lower().replace(" ", "_")
            create_strategy(self.strategies, strategy_key, task.description, task.tags)
            strategy = self.strategies["strategies"][strategy_key]
            steps = task.steps

        strategy_key = None
        for k, v in self.strategies.get("strategies", {}).items():
            if v is strategy:
                strategy_key = k
                break
        if not strategy_key:
            strategy_key = task.key.lower().replace(" ", "_")

        existing_ids = {a["id"] for a in strategy.get("approaches", [])}
        if approach_id not in existing_ids:
            add_approach(self.strategies, strategy_key, {
                "id": approach_id,
                "steps": steps,
            })

        started = time.time()
        step_results = []
        error = ""

        for step in steps:
            resolved = self._resolve_step(step, task)
            result = self.bridge.route([resolved])
            step_results.append(result)
            if not result.get("ok"):
                error = result.get("error", "step failed")
                break

        duration_ms = (time.time() - started) * 1000

        if not error and task.verification:
            success, detail = self._verify(task.verification)
        elif not error:
            success = all(r.get("ok") for r in step_results)
            detail = "all steps returned ok" if success else "step failure"
        else:
            success = False
            detail = error

        record_attempt(self.strategies, strategy_key, approach_id,
                       success, duration_ms, error if not success else "")

        result_obj = ExperimentResult(
            task_key=task.key,
            approach_id=approach_id,
            success=success,
            duration_ms=round(duration_ms, 1),
            error=error,
            verification_detail=detail,
        )

        self._log_experiment(result_obj)

        if success:
            save_strategies(self.strategies_path, self.strategies)
            result_obj.committed = self._git_commit(
                f"research: {task.description} -- {approach_id} succeeded"
            )
        else:
            save_strategies(self.strategies_path, self.strategies)

        return result_obj

    def _resolve_step(self, step: dict[str, Any],
                      task: Task) -> dict[str, Any]:
        """Replace template variables in step values."""
        resolved = {}
        for k, v in step.items():
            if isinstance(v, str):
                resolved[k] = v.replace("{app_name}", task.key).replace(
                    "{url}", task.description)
            elif isinstance(v, dict):
                resolved[k] = {
                    sk: (sv.replace("{app_name}", task.key).replace(
                        "{url}", task.description) if isinstance(sv, str) else sv)
                    for sk, sv in v.items()
                }
            else:
                resolved[k] = v
        return resolved

    def _verify(self, spec: dict[str, Any]) -> tuple[bool, str]:
        """Verify task completion using the specified method."""
        method = spec.get("method", "observe")

        if method == "title_contains":
            result = self.bridge.observe(uia=False, cached=True, max_age_ms=2000)
            if not result.get("ok"):
                return False, f"observe failed: {result.get('error')}"
            title = result.get("active", {}).get("title", "")
            expected = spec.get("expected", "")
            found = expected.lower() in title.lower()
            return found, f"title='{title}', expected='{expected}'"

        elif method == "ocr_contains":
            result = self.bridge.route([{"ocr_scan": {}}])
            if not result.get("ok"):
                return False, f"ocr failed: {result.get('error')}"
            steps = result.get("steps", [{}])
            elements = steps[0].get("elements", []) if steps else []
            all_text = " ".join(
                e.get("text", "") for e in elements
            ).lower()
            expected = spec.get("expected", "").lower()
            found = expected in all_text
            return found, f"ocr search for '{expected}': {'found' if found else 'not found'}"

        elif method == "element_exists":
            result = self.bridge.observe(uia=True, cached=False)
            if not result.get("ok"):
                return False, f"observe failed: {result.get('error')}"
            focused = result.get("focused_element", {})
            expected_name = spec.get("name", "")
            expected_role = spec.get("role", "")
            name_match = (not expected_name
                          or expected_name.lower() in focused.get("name", "").lower())
            role_match = (not expected_role
                          or expected_role.lower() in focused.get("control_type", "").lower())
            found = name_match and role_match
            return found, f"element check: name={focused.get('name')}, role={focused.get('control_type')}"

        return True, f"unknown method '{method}', assuming success"

    def _log_experiment(self, result: ExperimentResult) -> None:
        """Append experiment to the log file."""
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "experiment": self.experiment_count,
            **result.to_dict(),
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _git_commit(self, message: str) -> bool:
        """Commit strategies.json changes (the ratchet)."""
        try:
            subprocess.run(
                ["git", "add", str(self.strategies_path)],
                cwd=str(self.strategies_path.parent),
                capture_output=True, timeout=10,
            )
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(self.strategies_path.parent),
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def run_single(self, task_desc: str, steps: list[dict[str, Any]],
                   verification: dict[str, Any] | None = None,
                   tags: list[str] | None = None) -> ExperimentResult:
        """Convenience: run one experiment from raw parameters."""
        key = task_desc.lower().replace(" ", "_")
        task = Task(
            key=key,
            description=task_desc,
            steps=steps,
            verification=verification or {},
            tags=tags or [],
        )
        return self.run_experiment(task)

    def run_loop(self, max_experiments: int = 0, interval: float = 5.0) -> None:
        """Run experiments continuously from program.md tasks."""
        print(f"AutoResearch loop started. Program: {self.program_path}")
        print(f"Strategies: {self.strategies_path}")
        print(f"Goal: {self.program.get('goal', 'not set')}")
        print(f"Tasks: {len(self.program.get('tasks', []))}")
        print()

        count = 0
        while True:
            if max_experiments and count >= max_experiments:
                break

            tasks = self.program.get("tasks", [])
            if not tasks:
                print("No tasks in program.md. Waiting...")
                time.sleep(interval * 2)
                self.program = parse_program(self.program_path)
                continue

            for task_desc in tasks:
                if max_experiments and count >= max_experiments:
                    break

                strategy = find_strategy(self.strategies, task_desc)
                if strategy:
                    best = get_best_approach(strategy)
                    if best and best.get("success_rate", 0) > 0.95 and best.get("attempts", 0) >= 20:
                        continue

                key = task_desc.lower().replace(" ", "_")[:40]
                steps = self._infer_steps(task_desc)
                if not steps:
                    continue

                print(f"[{count + 1}] Experiment: {task_desc}")
                result = self.run_single(task_desc, steps)
                status = "OK" if result.success else "FAIL"
                print(f"     {status} ({result.duration_ms:.0f}ms) "
                      f"-- {result.verification_detail}")
                if result.committed:
                    print(f"     Committed to git")
                print()

                count += 1
                time.sleep(interval)

            self.strategies = load_strategies(self.strategies_path)
            self.program = parse_program(self.program_path)

            pruned = prune_strategies(self.strategies)
            if pruned:
                print(f"Pruned failed approaches: {pruned}")
                save_strategies(self.strategies_path, self.strategies)

        print(f"\nCompleted {count} experiments.")
        print(export_summary(self.strategies))

    def _infer_steps(self, task_desc: str) -> list[dict[str, Any]]:
        """Try to find steps for a task from existing strategies."""
        strategy = find_strategy(self.strategies, task_desc)
        if strategy:
            best = get_best_approach(strategy)
            if best:
                return best.get("steps", [])
        return []


def main():
    parser = argparse.ArgumentParser(description="Yaldabaoth AutoResearch Loop")
    parser.add_argument("--program", type=Path, default=DEFAULT_PROGRAM,
                        help="Path to program.md")
    parser.add_argument("--strategies", type=Path, default=DEFAULT_STRATEGIES,
                        help="Path to strategies.json")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG,
                        help="Path to experiment_log.jsonl")
    parser.add_argument("--command", type=Path, default=DEFAULT_COMMAND_PATH,
                        help="Path to command.json (Yaldabaoth bridge)")
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT_PATH,
                        help="Path to command.result.json (Yaldabaoth bridge)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single experiment, then exit")
    parser.add_argument("--max", type=int, default=0,
                        help="Max experiments (0 = unlimited)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Seconds between experiments")
    args = parser.parse_args()

    bridge = YaldBridge(args.command, args.result)
    loop = ResearchLoop(bridge, args.strategies, args.program, args.log)

    if args.once:
        tasks = loop.program.get("tasks", [])
        if tasks:
            steps = loop._infer_steps(tasks[0])
            if steps:
                result = loop.run_single(tasks[0], steps)
                print(json.dumps(result.to_dict(), indent=2))
            else:
                print(f"No known steps for: {tasks[0]}")
        else:
            print("No tasks in program.md")
    else:
        loop.run_loop(max_experiments=args.max, interval=args.interval)


if __name__ == "__main__":
    main()
