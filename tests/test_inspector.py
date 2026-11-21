"""Tests for window resolution and text formatting.

Pure logic: no Win32 calls, no real windows. `windows` is passed in explicitly.
"""

from __future__ import annotations

import pytest

from som_window_mcp import inspector, overlay


def window(hwnd, title, process="app.exe", **extra):
    data = {
        "hwnd": hwnd,
        "title": title,
        "process": process,
        "pid": 1,
        "class": "X",
        "rect": [0, 0, 100, 100],
        "occluded": False,
        "elevated": False,
        "foreground": False,
    }
    data.update(extra)
    return data


WINDOWS = [
    window(100, "System32", "explorer.exe", foreground=True),
    window(200, "Nikita-Pugach - Google Chrome", "chrome.exe"),
    window(300, "что известно о гта 6 - Google Chrome", "chrome.exe"),
    window(400, "AIDA64 Extreme", "aida64.exe", elevated=True),
]


# ---------------------------------------------------------------- resolution
@pytest.mark.parametrize("spec", ["100", "0x64", " 100 "])
def test_resolve_by_hwnd(spec):
    match, error = inspector.resolve_window(spec, WINDOWS)
    assert error is None
    assert match["hwnd"] == 100


def test_resolve_by_title_is_case_insensitive():
    match, error = inspector.resolve_window("system32", WINDOWS)
    assert error is None
    assert match["hwnd"] == 100


def test_resolve_by_process_name():
    match, error = inspector.resolve_window("explorer", WINDOWS)
    assert error is None
    assert match["hwnd"] == 100


def test_resolve_ambiguous_is_an_error_not_a_guess():
    match, error = inspector.resolve_window("chrome", WINDOWS)
    assert match is None
    assert error is not None
    assert "2 windows match" in error
    # both candidates must be listed so the caller can pick one
    assert "hwnd=200" in error and "hwnd=300" in error


def test_resolve_missing_lists_available_windows():
    match, error = inspector.resolve_window("definitely-not-open", WINDOWS)
    assert match is None
    assert "no window matches" in error
    assert "System32" in error


def test_resolve_unknown_hwnd():
    match, error = inspector.resolve_window("999", WINDOWS)
    assert match is None
    assert "no visible window with hwnd 999" in error


def test_resolve_empty_spec():
    match, error = inspector.resolve_window("   ", WINDOWS)
    assert match is None
    assert "empty" in error


# ------------------------------------------------------------------ listing
def test_format_window_list_shows_flags():
    text = inspector.format_window_list(WINDOWS, own_elevated=False)
    assert "4 visible window(s)" in text
    assert "NOT elevated" in text
    assert "AIDA64 Extreme" in text
    # the elevated window is marked as such
    aida_line = next(line for line in text.splitlines() if "aida64.exe" in line)
    assert " yes " in aida_line


# --------------------------------------------------------------------- tree
def sample_tree(nodes, truncated=False, visited=10):
    return {
        "nodes": nodes,
        "meta": {
            "visited": visited,
            "emitted": len(nodes),
            "depth_reached": 3,
            "elapsed_ms": 12.5,
            "truncated": truncated,
            "errors": 0,
        },
    }


def node(path, kind="Button", name="Save", rect=(10, 20, 90, 28), **extra):
    data = {"path": path, "type": kind, "name": name, "id": "", "rect": list(rect), "enabled": True}
    data.update(extra)
    return data


def test_format_tree_renders_one_line_per_element():
    result = sample_tree([node("0.1"), node("0.2", "Edit", "Поиск", value="abc")])
    text = inspector.format_tree(WINDOWS[0], result)
    assert 'window: "System32"' in text
    assert "0.1" in text and "Button" in text and '"Save"' in text
    assert 'value="abc"' in text
    assert "@10,20 90x28" in text


def test_format_tree_marks_truncation():
    text = inspector.format_tree(WINDOWS[0], sample_tree([node("0")], truncated=True))
    assert "truncated" in text.lower()


def test_format_tree_explains_empty_tree():
    text = inspector.format_tree(WINDOWS[0], sample_tree([], visited=1))
    assert "no interactive elements" in text
    assert "elevated" in text or "force-renderer-accessibility" in text


def test_format_tree_warns_when_window_is_covered():
    covered = window(1, "Hidden", occluded=True)
    text = inspector.format_tree(covered, sample_tree([node("0")]))
    assert "covered" in text


# ---------------------------------------------------------------------- som
def mark(number, **node_extra):
    data = node(f"0.{number}", **node_extra)
    return overlay.Mark(number=number, node=data, box=(0, 0, 10, 10))


def test_format_som_maps_numbers_to_screen_rects():
    marks = [mark(1, name="Свернуть", rect=(1527, 225, 60, 37))]
    text = inspector.format_som(WINDOWS[0], marks, {"interactive": 1, "visited": 5, "elapsed_ms": 3})
    assert "marks=1" in text
    assert "1" in text and '"Свернуть"' in text and "@1527,225 60x37" in text
    assert "Answer by mark number" in text


def test_format_som_falls_back_to_placeholder_for_unnamed():
    marks = [mark(3, name="")]
    text = inspector.format_som(WINDOWS[0], marks, {})
    assert "<noname>" in text
