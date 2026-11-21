"""Win32 layer: DPI awareness, window enumeration, window capture.

Everything here is deliberately dependency-light: ctypes + Pillow. No UI
Automation - that lives in the worker subprocess, because UIA calls can block
forever and must be killable.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

from PIL import Image

IS_WINDOWS = os.name == "nt"

try:  # ctypes.wintypes only exists on Windows; the fallback just type-checks.
    from ctypes import wintypes as wt
except Exception:  # pragma: no cover - non-Windows

    class _FallbackPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _FallbackWintypes:
        DWORD = ctypes.c_ulong
        WORD = ctypes.c_ushort
        UINT = ctypes.c_uint
        BOOL = ctypes.c_int
        LONG = ctypes.c_long
        HWND = ctypes.c_void_p
        HDC = ctypes.c_void_p
        HANDLE = ctypes.c_void_p
        LPARAM = ctypes.c_ssize_t
        LPWSTR = ctypes.c_wchar_p
        POINT = _FallbackPoint

    wt = _FallbackWintypes()


class _Unavailable:
    """Stand-in for the Win32 DLLs on other platforms.

    Attribute access returns another stub, so the module-level `argtypes = ...`
    declarations still execute; calling anything raises. That is what lets a
    package index install this server, start it and read its tool list on Linux
    while every tool refuses to run there - the same pattern other Windows-only
    MCP servers use for directory scanners.
    """

    def __getattr__(self, name):
        return _Unavailable()

    def __call__(self, *args, **kwargs):
        raise RuntimeError("som-window-mcp requires Windows 10/11")


# --------------------------------------------------------------------------
# DPI awareness must be set before any window or DC exists. UIA rects,
# captured pixels and mouse coordinates only agree if the process is
# per-monitor aware; otherwise everything is off by 25% on 150% displays.
# --------------------------------------------------------------------------
PER_MONITOR_AWARE_V2 = -4
if IS_WINDOWS:
    try:
        DPI_AWARENESS_OK = bool(
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(PER_MONITOR_AWARE_V2)
            )
        )
    except Exception:  # pragma: no cover - older Windows
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            DPI_AWARENESS_OK = True
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
            DPI_AWARENESS_OK = True

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    dwmapi = ctypes.windll.dwmapi
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
else:  # pragma: no cover - non-Windows
    DPI_AWARENESS_OK = False
    user32 = gdi32 = dwmapi = kernel32 = advapi32 = _Unavailable()

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_ID = 20
GA_ROOT = 2

# Shell and desktop plumbing that is never interesting to an agent.
SKIP_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "MultitaskingViewFrame",
    "ForegroundStaging",
    "XamlExplorerHostIslandWindow",
}


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

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


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


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


if hasattr(ctypes, "WINFUNCTYPE"):
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
else:  # pragma: no cover - non-Windows
    def WNDENUMPROC(callback):
        return callback

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
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
user32.GetForegroundWindow.restype = wt.HWND

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


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    if user32.GetClassNameW(hwnd, buf, 256):
        return buf.value
    return ""


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid: int) -> str | None:
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
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError(f"GetWindowRect failed for {hwnd:#x}")
    return rect


def visual_rect(hwnd: int) -> RECT:
    """Bounds without the invisible DWM resize border (Win10/11)."""
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
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    return root_window_at(cx, cy) != hwnd


def process_is_elevated(pid: int) -> bool | None:
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
                token,
                TOKEN_ELEVATION_ID,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(size),
            ):
                return None
            return bool(info.TokenIsElevated)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def own_is_elevated() -> bool:
    return bool(process_is_elevated(os.getpid()))


def list_windows(min_size: int = 40, include_desktop: bool = False) -> list[dict]:
    """Visible top-level windows in z-order, front to back."""
    foreground = user32.GetForegroundWindow()
    found: list[dict] = []

    def inspect(hwnd) -> dict | None:
        if not user32.IsWindowVisible(hwnd):
            return None
        if is_cloaked(hwnd):
            return None
        title = window_title(hwnd)
        if not title.strip():
            return None
        klass = window_class(hwnd)
        if not include_desktop and (klass in SKIP_CLASSES or title == "Program Manager"):
            return None
        rect = window_rect(hwnd)
        if rect.width < min_size or rect.height < min_size:
            return None

        pid = window_pid(hwnd)
        return {
            "hwnd": hwnd,
            "title": title,
            "process": process_name(pid),
            "pid": pid,
            "class": klass,
            "rect": list(rect.as_tuple()),
            "occluded": is_occluded(hwnd, rect),
            "elevated": process_is_elevated(pid),
            "foreground": hwnd == foreground,
        }

    def callback(hwnd, _lparam):
        # One hostile or dying window must never abort the whole enumeration.
        try:
            window = inspect(hwnd)
        except Exception:
            return True
        if window is not None:
            found.append(window)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)

    # Foreground first, then keep the OS z-order for the rest.
    found.sort(key=lambda w: (not w["foreground"],))
    return found


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------
@dataclass
class Capture:
    image: Image.Image
    printwindow_ok: bool
    window_rect: tuple[int, int, int, int]
    visual_rect: tuple[int, int, int, int]
    offset: tuple[int, int]

    @property
    def origin(self) -> tuple[int, int]:
        """Screen coordinates of image pixel (0, 0)."""
        return (self.visual_rect[0], self.visual_rect[1])


def capture(hwnd: int) -> Capture:
    rect = window_rect(hwnd)
    width, height = rect.width, rect.height
    if width <= 0 or height <= 0:
        raise RuntimeError(f"window {hwnd:#x} has an empty rect")

    screen_dc = user32.GetWindowDC(hwnd)
    if not screen_dc:
        raise RuntimeError(f"GetWindowDC failed for {hwnd:#x}")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(mem_dc, bitmap)
    try:
        # PW_RENDERFULLCONTENT asks the window itself to draw into our DC, so
        # occlusion does not matter. Minimised and GPU-only surfaces may still
        # come back blank - callers must check.
        ok = bool(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))

        header = BITMAPINFO()
        header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.bmiHeader.biWidth = width
        header.bmiHeader.biHeight = -height
        header.bmiHeader.biPlanes = 1
        header.bmiHeader.biBitCount = 32
        header.bmiHeader.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0) == 0:
            raise RuntimeError("GetDIBits failed")

        # BGRX drops alpha on purpose: PW_RENDERFULLCONTENT frequently leaves
        # alpha at zero, and keeping RGBA yields a fully transparent image.
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, screen_dc)

    vis = visual_rect(hwnd)
    offset = (vis.left - rect.left, vis.top - rect.top)
    if offset != (0, 0) or (vis.width, vis.height) != (width, height):
        image = image.crop(
            (offset[0], offset[1], offset[0] + vis.width, offset[1] + vis.height)
        )

    return Capture(
        image=image,
        printwindow_ok=ok,
        window_rect=rect.as_tuple(),
        visual_rect=vis.as_tuple(),
        offset=offset,
    )
