"""Step 0 probe for som-window-mcp.

Purpose: prove, on this machine, that we can
  1. enumerate real top-level windows (no UWP ghosts, no invisible ones),
  2. tell whether a window is currently covered by another window,
  3. capture that window's pixels with PrintWindow(PW_RENDERFULLCONTENT),
     even while it is covered.

No MCP, no UI Automation, no abstractions yet. If this file works, the whole
project is viable. If PrintWindow returns blank frames for the apps you care
about, the architecture has to change - better to learn that in 40 minutes
than on hour 15.

Usage:
    python probe.py                 # list windows, capture the largest one
    python probe.py --list          # list windows only
    python probe.py --match obs     # capture first window whose title matches
    python probe.py --index 3       # capture window by index from --list
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes as wt

from PIL import Image, ImageStat

# --------------------------------------------------------------------------
# DPI awareness. MUST be set before any window/DC is created.
#
# UIA element rects, PrintWindow output and mouse coordinates only agree when
# the process is per-monitor DPI aware. Without this you get the classic
# "clicks land 25% off" bug on 125%/150% scaling, and only there.
# --------------------------------------------------------------------------
PER_MONITOR_AWARE_V2 = -4
try:
    if not ctypes.windll.user32.SetProcessDpiAwarenessContext(
        ctypes.c_void_p(PER_MONITOR_AWARE_V2)
    ):
        raise OSError("SetProcessDpiAwarenessContext failed")
except Exception:  # older Windows
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOT = 2


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# --------------------------------------------------------------------------
# Explicit argtypes/restypes. Without them ctypes defaults to c_int and
# silently truncates 64-bit handles - the classic "works until it doesn't".
# --------------------------------------------------------------------------
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [wt.HDC, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.GetDIBits.argtypes = [
    wt.HDC,
    ctypes.c_void_p,
    wt.UINT,
    wt.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO),
    wt.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int

dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]


# --------------------------------------------------------------------------
# Window metadata
# --------------------------------------------------------------------------
def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def process_name(hwnd: int) -> str | None:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return None


def window_rect(hwnd: int) -> RECT:
    """Full window rect - this is the coordinate space PrintWindow renders in."""
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError(f"GetWindowRect failed for {hwnd:#x}")
    return rect


def visual_rect(hwnd: int) -> RECT:
    """Visual bounds without the invisible DWM resize border (Win10/11, ~8px).

    GetWindowRect includes that border, DWMWA_EXTENDED_FRAME_BOUNDS does not.
    Mixing the two is what produces the "everything is offset by 8px" bug.
    """
    rect = RECT()
    if (
        dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(RECT)
        )
        == 0
    ):
        return rect
    return window_rect(hwnd)


def is_cloaked(hwnd: int) -> bool:
    """UWP/ApplicationFrameHost leaves invisible ghost windows that still
    report as visible. Filtering them is what separates `list_windows` from
    a dump of garbage entries."""
    value = wt.DWORD()
    if (
        dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
        != 0
    ):
        return False
    return value.value != 0


def root_window_at(x: int, y: int) -> int:
    hwnd = user32.WindowFromPoint(wt.POINT(x, y))
    return user32.GetAncestor(hwnd, GA_ROOT) if hwnd else 0


def is_occluded(hwnd: int, rect: RECT) -> bool:
    """True if another window owns the centre of the target."""
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    return root_window_at(cx, cy) != hwnd


def list_windows(min_size: int = 40) -> list[dict]:
    found: list[dict] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = window_title(hwnd)
        if not title:
            return True
        if is_cloaked(hwnd):
            return True
        try:
            rect = window_rect(hwnd)
        except RuntimeError:
            return True
        if rect.width < min_size or rect.height < min_size:
            return True

        vis = visual_rect(hwnd)
        found.append(
            {
                "hwnd": hwnd,
                "title": title,
                "process": process_name(hwnd),
                "rect": [rect.left, rect.top, rect.width, rect.height],
                "visual_rect": [vis.left, vis.top, vis.width, vis.height],
                "border_offset": [vis.left - rect.left, vis.top - rect.top],
                "occluded": is_occluded(hwnd, rect),
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------
def capture(hwnd: int) -> tuple[Image.Image, bool]:
    rect = window_rect(hwnd)
    width, height = rect.width, rect.height
    if width <= 0 or height <= 0:
        raise RuntimeError(f"window {hwnd:#x} has an empty rect")

    screen_dc = user32.GetWindowDC(hwnd)
    if not screen_dc:
        raise RuntimeError("GetWindowDC failed")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(mem_dc, bitmap)
    try:
        # PW_RENDERFULLCONTENT asks the window itself to redraw into our DC,
        # so occlusion does not matter. Minimised windows are a different
        # story: they may have no layout at all and come back blank.
        ok = bool(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))

        header = BITMAPINFO()
        header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.bmiHeader.biWidth = width
        header.bmiHeader.biHeight = -height  # negative => top-down rows
        header.bmiHeader.biPlanes = 1
        header.bmiHeader.biBitCount = 32
        header.bmiHeader.biCompression = 0  # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0
        )
        if copied == 0:
            raise RuntimeError("GetDIBits failed")

        # BGRX deliberately drops the alpha channel: PW_RENDERFULLCONTENT very
        # often leaves alpha at 0, and keeping RGBA turns the whole capture
        # fully transparent (or white, after flattening).
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
        return image, ok
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, screen_dc)


def describe(image: Image.Image) -> dict:
    """Detect the black/white frame that PrintWindow hands back for surfaces
    it cannot render (GPU-accelerated apps, some UWP)."""
    grey = image.convert("L")
    stat = ImageStat.Stat(grey)
    low, high = grey.getextrema()
    return {
        "size": list(image.size),
        "mean": round(stat.mean[0], 1),
        "stddev": round(stat.stddev[0], 2),
        "extrema": [low, high],
        "blank": (high - low) < 8,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="som-window-mcp step 0 probe")
    parser.add_argument("--list", action="store_true", help="list windows only")
    parser.add_argument("--match", help="capture first title containing this text")
    parser.add_argument("--index", type=int, help="capture window by list index")
    parser.add_argument("--outdir", default="out")
    args = parser.parse_args(argv)

    windows = list_windows()
    # Largest first: usually the most interesting window on screen.
    windows.sort(key=lambda w: w["rect"][2] * w["rect"][3], reverse=True)

    print(json.dumps({"count": len(windows), "windows": windows}, ensure_ascii=True, indent=2))
    if args.list:
        return 0
    if not windows:
        print("no windows to capture", file=sys.stderr)
        return 1

    target = None
    if args.match:
        needle = args.match.lower()
        target = next((w for w in windows if needle in w["title"].lower()), None)
        if target is None:
            print(f"no window matching {args.match!r}", file=sys.stderr)
            return 1
    elif args.index is not None:
        target = windows[args.index]
    else:
        target = windows[0]

    image, printwindow_ok = capture(target["hwnd"])

    # PrintWindow renders into the full window rect, but the visible content is
    # inset by the invisible DWM resize border (9px on Win11 for Chrome). Left
    # uncropped, every capture carries a black frame and every coordinate is
    # off by that amount. Cropping makes image pixel (0,0) == visual rect (0,0),
    # so screen = visual_rect.xy + image_pixel, with no fudge factors.
    rect = window_rect(target["hwnd"])
    vis = visual_rect(target["hwnd"])
    offset = (vis.left - rect.left, vis.top - rect.top)
    if offset != (0, 0) or (vis.width, vis.height) != (rect.width, rect.height):
        image = image.crop((offset[0], offset[1], offset[0] + vis.width, offset[1] + vis.height))

    info = describe(image)

    os.makedirs(args.outdir, exist_ok=True)
    stem = (target["process"] or "window").replace(".exe", "")
    path = os.path.join(args.outdir, f"{stem}_{target['hwnd']:#x}.png")
    image.save(path)

    print(
        json.dumps(
            {
                "captured": {
                    "hwnd": target["hwnd"],
                    "title": target["title"],
                    "process": target["process"],
                    "occluded": target["occluded"],
                    "printwindow_returned_true": printwindow_ok,
                    "cropped_from": [rect.width, rect.height],
                    "border_offset": list(offset),
                    "path": os.path.abspath(path),
                    **info,
                }
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0 if not info["blank"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
