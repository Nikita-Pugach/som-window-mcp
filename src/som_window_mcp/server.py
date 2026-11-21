"""MCP server: read-only eyes for Windows desktop windows.

Three tools, no input injection, no clicks. The server can look, not touch.
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import Image, MCPServer

from . import capture as capture_mod
from . import inspector, overlay, win32
from .worker import UiaWorker, WorkerError

INSTRUCTIONS = """\
Read-only access to Windows desktop windows.

Typical flow:
1. list_windows -> pick a window, get its hwnd.
2. get_ui_tree(hwnd) -> compact list of buttons, edits, tabs with screen rects.
3. capture_window(hwnd) -> pixels of that window only, even if it is covered.
4. capture_som(hwnd) -> the same capture with numbered boxes on every control,
   for when you need to locate something visually or in a dense layout.

Nothing here clicks, types or moves the mouse. Coordinate space is physical
screen pixels, and captures report the screen position of image pixel (0,0).

Known limits: elevated windows need an elevated server; Chromium/Electron apps
expose no UI tree unless started with --force-renderer-accessibility (screenshots
still work); minimised windows and some GPU surfaces capture blank.
"""

mcp = MCPServer(
    "som-window-mcp",
    version="0.2.0",
    instructions=INSTRUCTIONS,
)

_worker = UiaWorker()


def _log(message: str) -> None:
    print(f"[som-window-mcp] {message}", file=sys.stderr, flush=True)


def _elevation_problem(window: dict) -> str | None:
    if window["elevated"] and not win32.own_is_elevated():
        return (
            f'window "{window["title"][:80]}" ({window["process"]}) runs elevated, '
            "but this MCP server does not. Windows blocks UI Automation and window "
            "capture across integrity levels, so the window cannot be read. Start the "
            "MCP server elevated (same integrity level) to inspect it."
        )
    return None


def _blank_message(window: dict, info: dict) -> str:
    return (
        f'capture of "{window["title"][:80]}" came back blank '
        f'(size {info["size"][0]}x{info["size"][1]}, stddev {info["stddev"]}). '
        "The window is probably minimised, or it renders through a GPU surface "
        "PrintWindow cannot read (some games, some UWP apps). Restore the window "
        "to the foreground and retry, or fall back to get_ui_tree."
    )


@mcp.tool()
def list_windows(include_desktop: bool = False) -> str:
    """List visible top-level windows, foreground first.

    Returns hwnd, process name, rect in physical screen pixels, whether the
    window is covered by another window (occluded), and whether it can only be
    read by an elevated server (elevated). Titles are localized by Windows.

    Use the hwnd with get_ui_tree and capture_window. hwnds are stable for the
    lifetime of a window but change when the app is restarted.
    """
    windows = win32.list_windows(include_desktop=include_desktop)
    if not windows:
        return "no visible windows found"
    return inspector.format_window_list(windows, win32.own_is_elevated())


@mcp.tool()
def get_ui_tree(
    window: str,
    max_depth: int = 8,
    max_nodes: int = 600,
    include_text: bool = False,
) -> str:
    """Read the UI Automation tree of one window as a compact text list.

    `window` is an hwnd from list_windows (e.g. "132456") or a unique part of a
    title or process name (e.g. "notepad"). ambiguous matches are an error, not
    a guess.

    Returns one line per interactive element: path, control type, name,
    automation id, screen rect. Invisible and zero-size elements are dropped.

    max_depth defaults to 8 and the walk is breadth-first, because shallow
    depth limits hide real controls (a calculator loses its keypad at depth 4,
    Explorer hides the file list at depth 6). max_nodes caps the walk: dense
    windows cost about 2.5 ms per node, so raise it deliberately.
    include_text also lists static text elements, which is useful for reading
    labels and status messages.

    Read-only. If the window runs elevated while this server does not, Windows
    blocks UIA and the tool reports that instead of returning an empty tree.
    """
    windows = win32.list_windows()
    match, error = inspector.resolve_window(window, windows)
    if error:
        return error
    assert match is not None

    problem = _elevation_problem(match)
    if problem:
        return problem

    try:
        result = _worker.tree(
            match["hwnd"],
            max_depth=max(0, min(max_depth, 12)),
            max_nodes=max(10, min(max_nodes, 20000)),
            include_text=include_text,
        )
    except WorkerError as exc:
        return f"UI Automation failed: {exc}"

    return inspector.format_tree(match, result)


@mcp.tool(structured_output=False)
def capture_window(window: str, max_side: int = 1600) -> list:
    """Capture one window's pixels, including while it is covered.

    Uses PrintWindow, which asks the window to render itself, so the window does
    not need to be in the foreground: a window hidden behind your terminal still
    captures cleanly and only that window's content is in the image.

    Returns an image plus a text block with the window title, the image size,
    and `origin` - the screen coordinate of image pixel (0,0), so any pixel can
    be mapped back to a screen position.

    max_side downscales large windows to keep image tokens sane (0 disables).
    Read-only. Minimised windows and some GPU-rendered surfaces capture blank,
    which is reported instead of a useless image.
    """
    windows = win32.list_windows()
    match, error = inspector.resolve_window(window, windows)
    if error:
        return [error]
    assert match is not None

    problem = _elevation_problem(match)
    if problem:
        return [problem]

    try:
        shot = win32.capture(match["hwnd"])
    except Exception as exc:
        return [f"capture failed: {exc}"]

    info = capture_mod.analyse(shot.image)
    if info["blank"]:
        return [_blank_message(match, info)]

    data, fmt = capture_mod.encode(shot.image, max_side=max_side)
    origin = shot.origin
    meta = (
        f'window: "{match["title"][:80]}" ({match["process"]}, hwnd={match["hwnd"]})\n'
        f"image: {info['size'][0]}x{info['size'][1]} {fmt}, {len(data) // 1024} KB\n"
        f"origin: {origin[0]},{origin[1]}  (screen position of image pixel 0,0)\n"
        f"screen = origin + pixel_offset\n"
        f"occluded: {'yes' if match['occluded'] else 'no'}"
        f"{'  (win, the window is covered)' if match['occluded'] else ''}\n"
        f"capture: PrintWindow{' (reported failure, image may be stale)' if not shot.printwindow_ok else ''}"
    )
    _log(f"captured hwnd={match['hwnd']} {info['size']} {fmt} {len(data)} bytes")
    return [Image(data=data, format=fmt), meta]


@mcp.tool(structured_output=False)
def capture_som(window: str, max_marks: int = 60, max_side: int = 1600) -> list:
    """Capture a window with numbered marks on every interactive element.

    This is the tool to use when you need to understand *where* things are, not
    just *what* they are. The accessibility tree decides the boxes, so marks are
    pixel-exact and never hallucinated, and every mark in the legend carries a
    real screen rect.

    Prefer this over capture_window when you plan to act on the result: answer
    with a mark number, or use the rect directly and skip vision entirely.

    max_marks caps how many controls are numbered (dense toolbars get crowded);
    the highest-value controls are kept - named ones first, then larger ones.
    max_side downscales the image before drawing, so badges stay sharp.

    Falls back to a plain unmarked capture with an explanation when the window
    exposes no UI tree (Chromium/Electron without --force-renderer-accessibility).
    """
    windows = win32.list_windows()
    match, error = inspector.resolve_window(window, windows)
    if error:
        return [error]
    assert match is not None

    problem = _elevation_problem(match)
    if problem:
        return [problem]

    try:
        shot = win32.capture(match["hwnd"])
    except Exception as exc:
        return [f"capture failed: {exc}"]

    info = capture_mod.analyse(shot.image)
    if info["blank"]:
        return [_blank_message(match, info)]

    tree: dict | None = None
    tree_error: str | None = None
    try:
        tree = _worker.tree(match["hwnd"], max_depth=8, max_nodes=600)
    except WorkerError as exc:
        tree_error = str(exc)

    nodes = tree["nodes"] if tree else []
    image, marks, factor = overlay.render(
        shot.image,
        nodes,
        shot.origin,
        max_marks=max(1, min(max_marks, 200)),
        max_side=max(1, min(max_side, 4000)),
    )
    # Drawing already downscaled, so encode at native size.
    data, fmt = capture_mod.encode(image, max_side=0)

    if marks:
        legend = inspector.format_som(match, marks, tree.get("meta", {}) if tree else {})
    else:
        reason = tree_error or "the UI tree exposed no interactive elements"
        legend = (
            f'window: "{match["title"][:80]}" ({match["process"]}, hwnd={match["hwnd"]})\n'
            f"no numbered marks: {reason}\n"
            "This is a plain capture. Chromium/Electron apps expose no tree unless "
            "relaunched with --force-renderer-accessibility."
        )

    meta = (
        f"image: {image.size[0]}x{image.size[1]} {fmt}, {len(data) // 1024} KB"
        + (f" (downscaled x{factor:.2f})" if factor != 1.0 else "")
        + f"\norigin: {shot.origin[0]},{shot.origin[1]}  (screen position of image pixel 0,0)"
        + "\nscreen = origin + pixel_offset"
    )
    _log(
        f"som hwnd={match['hwnd']} marks={len(marks)} "
        f"{image.size} {fmt} {len(data)} bytes"
    )
    return [Image(data=data, format=fmt), legend + "\n" + meta]


def main() -> None:
    _log("starting (stdio)")
    mcp.run()

if __name__ == "__main__":
    main()
