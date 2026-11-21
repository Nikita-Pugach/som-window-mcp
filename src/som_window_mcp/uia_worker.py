"""UIA worker process.

Runs as a separate interpreter on purpose: `GetChildren()` on a hung or
cross-integrity window can block forever inside COM, and no timeout in the
parent thread can cancel a blocked COM call. The only reliable escape is to
kill the process that made the call.

Protocol: one JSON object per line on stdin, one JSON object per line on
stdout. stdout belongs to the protocol, so it is swapped for stderr before
any third-party import to keep stray prints from corrupting the stream.

Kept dependency-free on purpose (stdlib + uiautomation only) so it can be
started by path, installed or not.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time

# UIA returns physical pixels only for DPI-aware clients. This must happen
# before the first UIA call.
PER_MONITOR_AWARE_V2 = -4
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(
        ctypes.c_void_p(PER_MONITOR_AWARE_V2)
    )
except Exception:  # pragma: no cover
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr  # anything else that prints must not corrupt the pipe

# Types an agent can actually act on. Everything else is a container.
INTERACTIVE = {
    "Button",
    "Edit",
    "TabItem",
    "MenuItem",
    "ListItem",
    "CheckBox",
    "ComboBox",
    "TreeItem",
    "Hyperlink",
    "RadioButton",
    "Slider",
    "Spinner",
    "SplitButton",
    "DataItem",
    "HeaderItem",
    "Thumb",
    "Document",
}
# Leaf types that are not worth descending into: pruning them saves a
# surprising number of cross-process calls on text-heavy windows.
PRUNE = {"Text", "Image", "ScrollBar"}
VALUE_TYPES = {"Edit", "ComboBox"}

MAX_NAME = 120


def reply(payload: dict) -> None:
    _PROTOCOL_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _PROTOCOL_OUT.flush()


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def control_type(ctrl) -> str:
    raw = safe(lambda: ctrl.ControlTypeName, "") or ""
    return raw[:-7] if raw.endswith("Control") else raw


def rect_of(ctrl) -> list[int] | None:
    # Offscreen controls report an all-zero rect, which is useless for clicking.
    rect = safe(lambda: ctrl.BoundingRectangle)
    if rect is None:
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return [rect.left, rect.top, width, height]


def walk(hwnd: int, max_depth: int, max_nodes: int, include_text: bool) -> dict:
    started = time.perf_counter()
    try:
        import uiautomation as auto
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"uiautomation import failed: {exc}"}

    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception as exc:
        return {"ok": False, "error": f"ControlFromHandle failed: {exc}"}
    if root is None:
        return {"ok": False, "error": "no UI Automation root for this window"}

    nodes: list[dict] = []
    stats = {
        "visited": 0,
        "emitted": 0,
        "no_rect": 0,
        "no_name": 0,
        "errors": 0,
        "depth_reached": 0,
        "truncated": False,
    }

    stack: list[tuple[object, str, int]] = [(root, "", 0)]
    while stack:
        if stats["visited"] >= max_nodes:
            stats["truncated"] = True
            break
        ctrl, path, depth = stack.pop()
        stats["visited"] += 1
        stats["depth_reached"] = max(stats["depth_reached"], depth)

        kind = control_type(ctrl)
        name = (safe(lambda: ctrl.Name, "") or "").strip()
        rect = rect_of(ctrl)
        if rect is None:
            stats["no_rect"] += 1

        if rect is not None:
            wanted = kind in INTERACTIVE or (include_text and name)
            if wanted:
                if not name:
                    stats["no_name"] += 1
                node = {
                    "path": path or "root",
                    "type": kind,
                    "name": name[:MAX_NAME],
                    "id": safe(lambda: ctrl.AutomationId, "") or "",
                    "rect": rect,
                    "enabled": safe(lambda: ctrl.IsEnabled, None),
                }
                if kind in VALUE_TYPES:
                    value = safe(lambda: ctrl.GetValuePattern().Value)
                    if value:
                        node["value"] = str(value)[:MAX_NAME]
                if safe(lambda: ctrl.IsOffscreen, False):
                    node["offscreen"] = True
                nodes.append(node)
                stats["emitted"] += 1

        if depth >= max_depth or kind in PRUNE:
            continue
        try:
            children = ctrl.GetChildren()
        except Exception:
            stats["errors"] += 1
            continue
        if not children:
            continue
        for index in range(len(children) - 1, -1, -1):
            child_path = f"{path}.{index}" if path else str(index)
            stack.append((children[index], child_path, depth + 1))

    # Reading order beats tree order for a human or a vision model looking at
    # the same window.
    nodes.sort(key=lambda n: (n["rect"][1], n["rect"][0]))

    return {
        "ok": True,
        "nodes": nodes,
        "meta": {
            **stats,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "root_type": control_type(root),
            "root_title": (safe(lambda: root.Name, "") or "")[:120],
        },
    }


def handle(request: dict) -> dict:
    cmd = request.get("cmd")
    if cmd == "ping":
        return {"ok": True, "pid": os.getpid()}
    if cmd == "tree":
        hwnd = request.get("hwnd")
        if not isinstance(hwnd, int):
            return {"ok": False, "error": "tree requires an integer hwnd"}
        return walk(
            hwnd,
            max_depth=int(request.get("max_depth", 6)),
            max_nodes=int(request.get("max_nodes", 600)),
            include_text=bool(request.get("include_text", False)),
        )
    return {"ok": False, "error": f"unknown command {cmd!r}"}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception as exc:
            reply({"ok": False, "error": f"bad request: {exc}"})
            continue
        try:
            reply(handle(request))
        except Exception as exc:  # never let the worker die silently
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
