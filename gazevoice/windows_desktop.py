"""Read Windows client rectangles in CSS-compatible logical screen coordinates.

No screenshots, titles, or camera access. DPI awareness is changed only on the
calling thread, then restored, never on Blender/Unity or the user's process.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes


def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    api.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    api.GetDpiForWindow.argtypes = [wintypes.HWND]
    api.GetDpiForWindow.restype = wintypes.UINT
    api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.IsIconic.argtypes = [wintypes.HWND]
    return api


@contextmanager
def _physical(api):
    before = api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    try:
        yield
    finally:
        if before:
            api.SetThreadDpiAwarenessContext(before)


def _uniform_scaling(api) -> bool:
    """Mixed-DPI desktop origins differ between browsers; don't guess them."""
    scales = []
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.GetDpiForMonitor.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE,
            wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        api.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, callback_type, wintypes.LPARAM]

        @callback_type
        def visit(handle, _dc, _rect, _data):
            x, y = wintypes.UINT(), wintypes.UINT()
            if shcore.GetDpiForMonitor(handle, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
                scales.append(x.value)
            else:
                scales.append(None)
            return True

        return bool(api.EnumDisplayMonitors(None, None, visit, 0)) and bool(scales) and None not in scales and len(set(scales)) == 1
    except OSError:
        return False


def frame(pid: int, width: int, height: int) -> tuple | None:
    api = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    candidates = []

    @callback_type
    def visit(handle, _):
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value != pid or not api.IsWindowVisible(handle) or api.IsIconic(handle):
            return True
        rect, point = wintypes.RECT(), wintypes.POINT()
        if api.GetClientRect(handle, ctypes.byref(rect)) and api.ClientToScreen(handle, ctypes.byref(point)):
            scale = (api.GetDpiForWindow(handle) or 96) / 96
            w, h = rect.right - rect.left, rect.bottom - rect.top
            # Blender's window dimensions vary with DPI awareness across releases.
            error = min(abs(w - width) + abs(h - height), abs(w / scale - width) + abs(h / scale - height))
            candidates.append((error, (point.x / scale, point.y / scale, w / scale, h / scale)))
        return True

    with _physical(api):
        # Uniform scaling has one screen-coordinate transform. With mixed DPI,
        # return no target so voice safely uses the current selection instead.
        if not _uniform_scaling(api):
            return None
        api.EnumWindows(visit, 0)
    if not candidates:
        return None
    error, result = min(candidates, key=lambda item: item[0])
    return result if error <= 8 else None


def viewport_area(reply: dict, rectangle: tuple) -> tuple:
    """Map Blender viewport pixels through its client content, including display scaling."""
    left, top, w, h = rectangle
    # Ratios use Blender's reported client dimensions. macOS retains its established formula.
    sx = w / reply["window_width"]
    sy = h / reply["window_height"]
    return (left + reply["x"] / reply["scale"] * sx,
            top + h - (reply["y"] + reply["height"]) / reply["scale"] * sy,
            reply["width"] / reply["scale"] * sx, reply["height"] / reply["scale"] * sy)
