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
from collections import deque

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
# Containers worth addressable by path, for inspect_element: a table or a long
# list is better read in full than numbered row by row.
CONTAINER_TYPES = {"List", "Tree", "Table", "DataGrid", "Document"}
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


def walk(
    hwnd: int,
    max_depth: int,
    max_nodes: int,
    include_text: bool,
    include_containers: bool = False,
) -> dict:
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

    # Breadth-first on purpose: with a fixed node budget, breadth reaches the
    # shallow controls an agent actually wants (toolbars, tabs, list items)
    # before depth-first disappears into the first menu's internals.
    for ctrl, path, depth in iter_nodes(root, max_depth, max_nodes, stats):
        kind = control_type(ctrl)
        name = (safe(lambda: ctrl.Name, "") or "").strip()
        rect = rect_of(ctrl)
        if rect is None:
            stats["no_rect"] += 1

        if rect is not None:
            wanted = (
                kind in INTERACTIVE
                or (include_text and name)
                or (include_containers and kind in CONTAINER_TYPES)
            )
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


def iter_nodes(root, max_depth: int, max_nodes: int, stats: dict):
    """Yield (control, path, depth) breadth-first, updating `stats`."""
    queue: deque[tuple[object, str, int]] = deque([(root, "", 0)])
    while queue:
        if stats["visited"] >= max_nodes:
            stats["truncated"] = True
            return
        ctrl, path, depth = queue.popleft()
        stats["visited"] += 1
        stats["depth_reached"] = max(stats["depth_reached"], depth)
        yield ctrl, path, depth

        kind = control_type(ctrl)
        if depth >= max_depth or kind in PRUNE:
            continue
        try:
            children = ctrl.GetChildren()
        except Exception:
            stats["errors"] += 1
            continue
        if not children:
            continue
        for index in range(len(children)):
            child_path = f"{path}.{index}" if path else str(index)
            queue.append((children[index], child_path, depth + 1))


def _text_of(ctrl, max_chars: int) -> str | None:
    """Best-effort text extraction, cheapest pattern first."""
    value = safe(lambda: ctrl.GetValuePattern().Value)
    if value:
        return str(value)[:max_chars]
    try:
        pattern = ctrl.GetTextPattern()
    except Exception:
        pattern = None
    if pattern is not None:
        text = safe(lambda: pattern.GetText(max_chars))
        if text:
            return str(text)[:max_chars]
    legacy = safe(lambda: ctrl.GetLegacyIAccessiblePattern().Value)
    if legacy:
        return str(legacy)[:max_chars]
    return None


def element(hwnd: int, path: str, max_depth: int, max_nodes: int, max_chars: int) -> dict:
    """Describe a single element addressed by a path from `walk`."""
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

    wanted = path if path not in ("", "root") else ""
    stats = {"visited": 0, "errors": 0, "depth_reached": 0, "truncated": False}

    target = None
    for ctrl, node_path, _depth in iter_nodes(root, max_depth, max_nodes, stats):
        if node_path == wanted:
            target = ctrl
            break
    if target is None:
        return {
            "ok": False,
            "error": (
                f"no element at path {path!r} within depth {max_depth} and "
                f"{stats['visited']} visited nodes - the UI may have changed; "
                "call get_ui_tree again"
            ),
        }

    kind = control_type(target)
    info = {
        "path": path or "root",
        "type": kind,
        "name": (safe(lambda: target.Name, "") or "")[:MAX_NAME],
        "id": safe(lambda: target.AutomationId, "") or "",
        "class": safe(lambda: target.ClassName, "") or "",
        "rect": rect_of(target),
        "enabled": safe(lambda: target.IsEnabled, None),
        "offscreen": safe(lambda: target.IsOffscreen, None),
    }

    text = _text_of(target, max_chars)
    if text:
        info["text"] = text
        info["text_truncated"] = len(text) >= max_chars

    children: list[dict] = []
    try:
        raw_children = target.GetChildren()
    except Exception:
        raw_children = []
    for child in raw_children[:80]:
        rect = rect_of(child)
        children.append(
            {
                "type": control_type(child),
                "name": (safe(lambda c=child: c.Name, "") or "")[:MAX_NAME],
                "id": safe(lambda c=child: c.AutomationId, "") or "",
                "rect": rect,
            }
        )

    return {
        "ok": True,
        "element": info,
        "children": children,
        "child_count": len(raw_children),
        "meta": {
            **stats,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
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
            max_depth=int(request.get("max_depth", 8)),
            max_nodes=int(request.get("max_nodes", 600)),
            include_text=bool(request.get("include_text", False)),
            include_containers=bool(request.get("include_containers", False)),
        )
    if cmd == "element":
        hwnd = request.get("hwnd")
        if not isinstance(hwnd, int):
            return {"ok": False, "error": "element requires an integer hwnd"}
        return element(
            hwnd,
            str(request.get("path", "")),
            max_depth=int(request.get("max_depth", 8)),
            max_nodes=int(request.get("max_nodes", 600)),
            max_chars=int(request.get("max_chars", 4000)),
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
