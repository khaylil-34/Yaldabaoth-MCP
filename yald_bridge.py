"""Shared JSON file bridge for Yaldabaoth consumers."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class YaldBridge:
    def __init__(self, command_path: str | Path, result_path: str | Path,
                 timeout: float = 30.0):
        self.command_path = Path(command_path)
        self.result_path = Path(result_path)
        self.timeout = timeout

    def send(self, payload: dict[str, Any], *, wait: bool = True,
             timeout: float | None = None) -> dict[str, Any]:
        timeout = timeout if timeout is not None else self.timeout
        cmd_id = f"vk-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        payload = {"id": cmd_id, **payload}
        atomic_write(self.command_path, json.dumps(payload, ensure_ascii=False))
        if not wait:
            return {"ok": True, "id": cmd_id, "queued": True, **payload}

        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                result = json.loads(self.result_path.read_text(encoding="utf-8"))
                if result.get("id") == cmd_id:
                    return result
                last = result
            except FileNotFoundError:
                pass
            except Exception as e:
                last = {"error": str(e)}
            time.sleep(0.005)
        return {"ok": False, "id": cmd_id,
                "error": "timeout waiting for Yaldabaoth daemon result",
                "last": last}

    def route(self, steps: list[dict[str, Any]], *, wait: bool = True,
              timeout: float | None = None) -> dict[str, Any]:
        return self.send({"steps": steps}, wait=wait, timeout=timeout)

    def observe(self, *, windows: bool = False, uia: bool = True,
                cached: bool = False, max_age_ms: int | None = None,
                wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        obs: dict[str, Any] = {"windows": windows, "uia": uia, "cached": cached}
        if max_age_ms is not None:
            obs["max_age_ms"] = max_age_ms
        return self.send({"observe": obs}, wait=wait, timeout=timeout)
