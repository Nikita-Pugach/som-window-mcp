"""Presentation layer: window resolution and compact text formatting.

The whole point of the project is that an agent reads a *small* amount of text
instead of guessing from pixels, so formatting lives in one place and stays
deliberately terse.
"""

from __future__ import annotations

from . import win32

MAX_TITLE = 80
MAX_NAME = 60


def resolve_window(
    spec: str, windows: list[dict] | None = None
) -> tuple[dict | None, str | None]:
    """Find exactly one window by hwnd or by title/process substring.

    Ambiguity is an error, never a silent "first match": a wrong window means
    a wrong screenshot and, later, a click in the wrong place.
    """
    spec = str(spec).strip()
    if not spec:
        return None, "window spec is empty; pass an hwnd or part of a title"

    windows = win32.list_windows() if windows is None else windows

    hwnd: int | None = None
    try:
        if spec.lower().startswith("0x"):
            hwnd = int(spec, 16)
        elif spec.isdigit():
            hwnd = int(spec)
    except ValueError:
        hwnd = None

    if hwnd is not None:
        match = next((w for w in windows if w["hwnd"] == hwnd), None)
        if match is not None:
            return match, None
        return None, (
            f"no visible window with hwnd {hwnd}. Call list_windows to get current hwnds."
        )

    needle = spec.lower()
    matches = [
        w
        for w in windows
        if needle in w["title"].lower() or needle in (w["process"] or "").lower()
    ]
    if not matches:
        available = "\n".join(
            f"  hwnd={w['hwnd']:<10} {w['process'] or '?':<22} {w['title'][:MAX_TITLE]}"
            for w in windows[:10]
        )
        return None, (
            f"no window matches {spec!r}. Visible windows:\n{available}"
        )
    if len(matches) > 1:
        listing = "\n".join(
            f"  hwnd={w['hwnd']:<10} {w['process'] or '?':<22} {w['title'][:MAX_TITLE]}"
            for w in matches[:10]
        )
        return None, (
            f"{len(matches)} windows match {spec!r}; pass an hwnd instead:\n{listing}"
        )
    return matches[0], None


def format_window_list(windows: list[dict], own_elevated: bool) -> str:
    header = [
        f"{len(windows)} visible window(s), foreground first.",
        "rect = physical screen pixels (x,y,width,height). "
        f"This server is {'elevated' if own_elevated else 'NOT elevated'}.",
        "occluded = covered by another window; elevated = only readable by an elevated server.",
        "",
        f"{'hwnd':<10} {'process':<24} {'occl':<5} {'elev':<5} {'rect':<22} title",
    ]
    rows = []
    for w in windows:
        rect = w["rect"]
        rows.append(
            f"{w['hwnd']:<10} {(w['process'] or '?')[:24]:<24} "
            f"{'yes' if w['occluded'] else 'no':<5} "
            f"{'?' if w['elevated'] is None else ('yes' if w['elevated'] else 'no'):<5} "
            f"{f'{rect[0]},{rect[1]} {rect[2]}x{rect[3]}':<22} "
            f"{w['title'][:MAX_TITLE]}"
        )
    return "\n".join(header + rows)


def format_tree(window: dict, result: dict) -> str:
    meta = result.get("meta", {})
    nodes = result.get("nodes", [])
    lines = [
        f'window: "{window["title"][:MAX_TITLE]}" ({window["process"]}, hwnd={window["hwnd"]})',
        f'visited={meta.get("visited", 0)} shown={meta.get("emitted", 0)} '
        f'depth_reached={meta.get("depth_reached", 0)} '
        f'time={meta.get("elapsed_ms", 0)}ms '
        f'truncated={"yes" if meta.get("truncated") else "no"}',
    ]
    if window["occluded"]:
        lines.append("note: window is covered by another window (tree is still valid).")

    if not nodes:
        lines.append("")
        lines.append("no interactive elements found.")
        if meta.get("visited", 0) <= 2:
            lines.append(
                "Only the window root was exposed. Likely causes: the app is elevated "
                "(run the server elevated), it uses a non-UIA toolkit (Qt/Delphi/games), "
                "or it is a Chromium/Electron app that needs "
                "--force-renderer-accessibility."
            )
        else:
            lines.append(
                "The tree was readable but contained no controls matching the filter."
            )
        return "\n".join(lines)

    lines.append("")
    for node in nodes:
        name = node.get("name") or "<noname>"
        if len(name) > MAX_NAME:
            name = name[:MAX_NAME] + "..."
        rect = node["rect"]
        extras = []
        if node.get("id"):
            extras.append(f"id={node['id']}")
        if node.get("value"):
            extras.append(f'value="{node["value"][:40]}"')
        if node.get("offscreen"):
            extras.append("offscreen")
        if node.get("enabled") is False:
            extras.append("disabled")
        suffix = ("  " + " ".join(extras)) if extras else ""
        lines.append(
            f"{node['path']:<9} {node['type']:<13} \"{name}\" "
            f"@{rect[0]},{rect[1]} {rect[2]}x{rect[3]}{suffix}"
        )

    if meta.get("truncated"):
        lines.append("")
        lines.append(
            "truncated: budget reached. Raise max_nodes, or lower max_depth to focus."
        )
    return "\n".join(lines)


def format_element(window: dict, result: dict) -> str:
    """Detailed view of one element: value, text, and its immediate children."""
    element = result.get("element", {})
    meta = result.get("meta", {})
    rect = element.get("rect") or [0, 0, 0, 0]
    name = element.get("name") or "<noname>"
    lines = [
        f'window: "{window["title"][:MAX_TITLE]}" ({window["process"]}, hwnd={window["hwnd"]})',
        f'element: {element.get("type", "?")} "{name[:MAX_NAME]}" path={element.get("path")} '
        f"@{rect[0]},{rect[1]} {rect[2]}x{rect[3]}",
    ]

    extras = []
    if element.get("id"):
        extras.append(f"id={element['id']}")
    if element.get("class"):
        extras.append(f"class={element['class']}")
    if element.get("enabled") is False:
        extras.append("disabled")
    if element.get("offscreen"):
        extras.append("offscreen")
    if extras:
        lines.append("  " + " ".join(extras))

    text = element.get("text")
    if text:
        lines.append("")
        lines.append(
            "text:" + (" (truncated by max_chars)" if element.get("text_truncated") else "")
        )
        # Keep the output bounded even when the control holds a whole document.
        text_lines = text.splitlines()
        lines.extend(text_lines[:60])
        if len(text_lines) > 60:
            lines.append(f"... {len(text_lines) - 60} more lines")

    children = result.get("children") or []
    if children:
        total = result.get("child_count", len(children))
        lines.append("")
        lines.append(f"children: {total} (showing {min(len(children), 40)})")
        for child in children[:40]:
            child_rect = child.get("rect") or [0, 0, 0, 0]
            child_name = (child.get("name") or "<noname>")[:MAX_NAME]
            lines.append(
                f"  {child.get('type', '?'):<12} \"{child_name}\" "
                f"@{child_rect[0]},{child_rect[1]} {child_rect[2]}x{child_rect[3]}"
            )
        if len(children) > 40:
            lines.append(f"  ... {len(children) - 40} more")

    lines.append("")
    lines.append(
        f"visited={meta.get('visited', 0)} nodes, {meta.get('elapsed_ms', 0)}ms"
    )
    return "\n".join(lines)


def format_som(window: dict, marks: list, meta: dict) -> str:
    """Legend for the numbered overlay. Numbers map to screen rects."""
    lines = [
        f'window: "{window["title"][:MAX_TITLE]}" ({window["process"]}, hwnd={window["hwnd"]})',
        f"marks={len(marks)} of {meta.get('interactive', len(marks))} interactive elements "
        f"(visited={meta.get('visited', 0)}, {meta.get('elapsed_ms', 0)}ms)"
        + ("  truncated" if meta.get("truncated") else ""),
    ]
    if window["occluded"]:
        lines.append("note: window is covered by another window (capture is still valid).")
    lines.append('')
    for mark in marks:
        node = mark.node
        name = node.get("name") or "<noname>"
        if len(name) > MAX_NAME:
            name = name[:MAX_NAME] + "..."
        rect = node["rect"]
        extras = []
        if node.get("id"):
            extras.append(f"id={node['id']}")
        if node.get("value"):
            extras.append(f'value="{node["value"][:40]}"')
        if node.get("enabled") is False:
            extras.append("disabled")
        suffix = ("  " + " ".join(extras)) if extras else ""
        lines.append(
            f"{mark.number:>2}  {node['path']:<9} {node['type']:<13} \"{name}\" "
            f"@{rect[0]},{rect[1]} {rect[2]}x{rect[3]}{suffix}"
        )
    lines.append('')
    lines.append(
        "Answer by mark number. The rects above are screen coordinates, so a "
        "clicker can act on them without reading the image at all."
    )
    return "\n".join(lines)
