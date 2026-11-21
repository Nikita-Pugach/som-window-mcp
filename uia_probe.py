"""Step 0.5 probe: measure the Windows UI Automation tree of real apps.

We already know we can *see* a window. The open question is whether the
accessibility tree of a given app is usable at all, and how expensive it is
to read. Both answers vary wildly:

* Chrome / Electron  - rich tree, but accessibility turns on lazily, so the
                       first walk can cost seconds.
* Qt (CrystalDiskInfo) - partial tree, many controls anonymous.
* Delphi (AIDA64)    - often a flat or empty tree.
* plain Win32 dialog - small but perfect tree.

Usage:
    python uia_probe.py --match AIDA64
    python uia_probe.py --match CrystalDiskInfo --depth 4 --passes 2
    python uia_probe.py --all --depth 3
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from collections import Counter
from ctypes import wintypes as wt

import uiautomation as auto

import probe  # window enumeration, DPI setup, process names

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
user32 = ctypes.windll.user32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.OpenProcessToken.restype = wt.BOOL
advapi32.GetTokenInformation.argtypes = [
    wt.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
advapi32.GetTokenInformation.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

# Types worth marking in a SoM overlay. Everything else is a container.
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
}
TEXTUAL = {"Text", "Document"}


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_is_elevated(pid: int) -> bool | None:
    """None == unknown (process denied access)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            info = TOKEN_ELEVATION()
            size = wt.DWORD(ctypes.sizeof(info))
            if not advapi32.GetTokenInformation(
                token, TokenElevation, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(size)
            ):
                return None
            return bool(info.TokenIsElevated)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def control_type(ctrl) -> str:
    raw = safe(lambda: ctrl.ControlTypeName, "") or ""
    return raw[:-7] if raw.endswith("Control") else raw


def rect_of(ctrl) -> list[int] | None:
    rect = safe(lambda: ctrl.BoundingRectangle)
    if rect is None:
        return None
    return [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]


def child_of(hwnd: int):
    control = safe(lambda: auto.ControlFromHandle(hwnd))
    if control is None:
        control = safe(lambda: auto.Control(Handle=hwnd))
    return control


def walk(root, max_depth: int, max_nodes: int) -> dict:
    started = time.perf_counter()
    stats = {
        "nodes": 0,
        "named": 0,
        "interactive": 0,
        "interactive_named": 0,
        "textual": 0,
        "offscreen": 0,
        "depth_reached": 0,
        "truncated": False,
        "errors": 0,
    }
    types: Counter[str] = Counter()
    samples: list[dict] = []

    def visit(ctrl, depth: int) -> None:
        if stats["nodes"] >= max_nodes:
            stats["truncated"] = True
            return
        stats["nodes"] += 1
        stats["depth_reached"] = max(stats["depth_reached"], depth)

        kind = control_type(ctrl)
        types[kind] += 1
        name = safe(lambda: ctrl.Name, "") or ""
        if name.strip():
            stats["named"] += 1
        if kind in INTERACTIVE:
            stats["interactive"] += 1
            if name.strip():
                stats["interactive_named"] += 1
        if kind in TEXTUAL:
            stats["textual"] += 1
        if safe(lambda: ctrl.IsOffscreen, False):
            stats["offscreen"] += 1

        if kind in INTERACTIVE and len(samples) < 40:
            samples.append(
                {
                    "type": kind,
                    "name": name[:60],
                    "id": (safe(lambda: ctrl.AutomationId, "") or "")[:40],
                    "rect": rect_of(ctrl),
                    "enabled": safe(lambda: ctrl.IsEnabled, None),
                    "offscreen": safe(lambda: ctrl.IsOffscreen, None),
                }
            )

        if depth >= max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            stats["errors"] += 1
            return
        if not children:
            return
        for child in children:
            visit(child, depth + 1)

    visit(root, 0)
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "elapsed_ms": round(elapsed, 1),
        **stats,
        "types": dict(types.most_common(12)),
        "samples": samples,
    }


def probe_window(window: dict, depth: int, passes: int, max_nodes: int) -> dict:
    hwnd = window["hwnd"]
    pid = window_pid(hwnd)
    elevated = process_is_elevated(pid)
    root = child_of(hwnd)
    result = {
        "title": window["title"][:70],
        "process": window["process"],
        "hwnd": hwnd,
        "occluded": window["occluded"],
        "elevated": elevated,
        "uia_root_found": root is not None,
    }
    if root is None:
        result["note"] = "no UIA root: app exposes nothing, or UIPI blocks this process"
        return result

    runs = []
    for index in range(passes):
        stats = walk(root, depth, max_nodes)
        runs.append({"pass": index + 1, "elapsed_ms": stats.pop("elapsed_ms"), **stats})
    result["runs"] = runs
    result["samples"] = runs[-1]["samples"] if runs else []
    # keep output small: samples are only printed for the final pass
    for run in runs:
        run.pop("samples", None)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UIA tree probe")
    parser.add_argument("--match", action="append", default=[], help="title substring (repeatable)")
    parser.add_argument("--process", action="append", default=[], help="process name substring (repeatable)")
    parser.add_argument("--all", action="store_true", help="probe every visible window")
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--passes", type=int, default=2, help="cold vs warm timing")
    parser.add_argument("--max-nodes", type=int, default=4000)
    parser.add_argument("--json", help="write full result to this file")
    args = parser.parse_args(argv)

    windows = probe.list_windows()
    windows.sort(key=lambda w: -(w["rect"][2] * w["rect"][3]))

    if args.match:
        needles = [m.lower() for m in args.match]
        selected = [w for w in windows if any(n in w["title"].lower() for n in needles)]
    else:
        selected = []

    if args.process:
        processes = [p.lower() for p in args.process]
        for window in windows:
            if window in selected:
                continue
            if any(p in str(window["process"]).lower() for p in processes):
                selected.append(window)

    if not selected and args.all:
        selected = windows
    elif not selected and not args.process and not args.match:
        selected = windows[:3]

    if not selected:
        print("no matching windows", file=sys.stderr)
        return 1

    results = [probe_window(w, args.depth, args.passes, args.max_nodes) for w in selected]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)

    for item in results:
        print(f"\n=== {item['process']} | {item['title']}")
        print(f"    elevated={item['elevated']} occluded={item['occluded']} root={item['uia_root_found']}")
        for run in item.get("runs", []):
            print(
                f"    pass{run['pass']}: {run['elapsed_ms']:>8.1f} ms  nodes={run['nodes']:<5} "
                f"named={run['named']:<5} interact={run['interactive']:<4} "
                f"inter_named={run['interactive_named']:<4} depth={run['depth_reached']} "
                f"err={run['errors']}"
            )
        if item.get("runs"):
            print(f"    types: {item['runs'][-1]['types']}")
        if item.get("note"):
            print(f"    note: {item['note']}")
        for sample in item.get("samples", [])[:12]:
            name = sample["name"] or "<no name>"
            print(f"      - {sample['type']:<14} {name:<32} id={sample['id']:<24} rect={sample['rect']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
