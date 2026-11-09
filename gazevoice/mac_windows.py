"""Where a window is on this Mac's screens, and how big a screen is, as macOS itself says. Standard library only.

Asking for a window's owner, layer and frame needs no permission: only windows' titles and pictures do. Nor does
asking for a screen's size.
"""

from __future__ import annotations

import ctypes
import math
from typing import Sequence

MAX_TITLE_BAR = 64  # points: how much taller than its content a window's frame can be

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_EVERY_WINDOW = 0  # kCGWindowListOptionAll, so a window in another Space is found too
_INT64 = 4  # kCFNumberSInt64Type

_loaded = None


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [("origin", _Point), ("size", _Size)]


def _libraries():
    global _loaded
    if _loaded is None:
        cg, cf = ctypes.CDLL(_CORE_GRAPHICS), ctypes.CDLL(_CORE_FOUNDATION)
        pointer = ctypes.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes, cg.CGWindowListCopyWindowInfo.restype = [ctypes.c_uint32, ctypes.c_uint32], pointer
        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [pointer, ctypes.POINTER(_Rect)]
        cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
        cf.CFArrayGetCount.argtypes, cf.CFArrayGetCount.restype = [pointer], ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes, cf.CFArrayGetValueAtIndex.restype = [pointer, ctypes.c_long], pointer
        cf.CFDictionaryGetValue.argtypes, cf.CFDictionaryGetValue.restype = [pointer, pointer], pointer
        cf.CFNumberGetValue.argtypes = [pointer, ctypes.c_long, pointer]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFRelease.argtypes, cf.CFRelease.restype = [pointer], None
        display = ctypes.c_uint32
        cg.CGGetDisplaysWithPoint.argtypes = [_Point, ctypes.c_uint32, ctypes.POINTER(display), ctypes.POINTER(ctypes.c_uint32)]
        cg.CGGetDisplaysWithPoint.restype = ctypes.c_int32
        cg.CGDisplayScreenSize.argtypes, cg.CGDisplayScreenSize.restype = [display], _Size  # millimetres
        cg.CGDisplayBounds.argtypes, cg.CGDisplayBounds.restype = [display], _Rect  # points
        keys = tuple(pointer.in_dll(cg, name).value for name in ("kCGWindowOwnerPID", "kCGWindowLayer", "kCGWindowBounds"))
        _loaded = cg, cf, keys
    return _loaded


def list_windows() -> list[dict]:
    """Every window on this Mac, frontmost first: `pid`, `layer`, and `x`, `y`, `width`, `height` in points."""
    cg, cf, (owner, layer, bounds) = _libraries()

    def number(window, key) -> int | None:
        value = ctypes.c_int64()
        ref = cf.CFDictionaryGetValue(window, key)
        return value.value if ref and cf.CFNumberGetValue(ref, _INT64, ctypes.byref(value)) else None

    every = cg.CGWindowListCopyWindowInfo(_EVERY_WINDOW, 0)
    if not every:
        return []
    found = []
    try:
        for index in range(cf.CFArrayGetCount(every)):
            window = cf.CFArrayGetValueAtIndex(every, index)
            pid, level, frame = number(window, owner), number(window, layer), cf.CFDictionaryGetValue(window, bounds)
            rect = _Rect()
            if pid is None or level is None or not frame:
                continue
            if cg.CGRectMakeWithDictionaryRepresentation(frame, ctypes.byref(rect)):
                found.append({"pid": pid, "layer": level, "x": rect.origin.x, "y": rect.origin.y,
                              "width": rect.size.width, "height": rect.size.height})
    finally:
        cf.CFRelease(every)
    return found


def pick_window(windows: Sequence[dict], pid: int, width: int, height: int) -> tuple[float, float, float, float] | None:
    """(x, y, width, height) of the window of `pid` whose content is `width` by `height`, or None.

    A program can own other windows, like the strips macOS keeps for its menu bar. The size tells them apart.
    """
    for window in windows:
        if window["pid"] == pid and window["layer"] == 0 and abs(window["width"] - width) <= 1 \
                and 0 <= window["height"] - height <= MAX_TITLE_BAR:
            return window["x"], window["y"], window["width"], window["height"]
    return None


GUESSED_POINTS_PER_MM = 72 / 25.4  # 2.835: what CoreGraphics works a display's size out at when the display doesn't say
PAGE_WIDTH_CM = (15.0, 150.0)  # a calibration page narrower or wider than this means the screen's size was wrong


def cm_per_point(size_mm: tuple[float, float], size_points: tuple[float, float]) -> float | None:
    """How many centimetres one point is on a screen `size_mm` millimetres and `size_points` points across (width, then
    height), or None when that can't be trusted.

    It is measured along the diagonal, so that a turned display can't set one's width against the other's height. None
    when a size makes no sense (macOS says 0 by 0 for a display it knows no size for), and when the millimetres are
    only CoreGraphics' guess: with no size from the display, it works one out at 72 dots an inch.
    """
    numbers = [*size_mm, *size_points]
    if len(numbers) != 4 or not all(math.isfinite(n) and n > 0 for n in numbers):
        return None
    millimetres, points = math.hypot(*size_mm), math.hypot(*size_points)
    if math.isclose(millimetres, points / GUESSED_POINTS_PER_MM, rel_tol=0.02):
        return None
    return millimetres / 10 / points


def _screen_size(x: float, y: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """((width, height) in millimetres, (width, height) in points) of the display under the point (x, y), in the points
    macOS places windows by. None if no display is there."""
    cg, _, _ = _libraries()
    found, count = ctypes.c_uint32(), ctypes.c_uint32()
    if cg.CGGetDisplaysWithPoint(_Point(x, y), 1, ctypes.byref(found), ctypes.byref(count)) != 0 or count.value < 1:
        return None
    millimetres, points = cg.CGDisplayScreenSize(found.value), cg.CGDisplayBounds(found.value).size
    return (millimetres.width, millimetres.height), (points.width, points.height)


def page_width_cm(left: float, top: float, width: float, height: float) -> float | None:
    """How wide the calibration page at (left, top, width, height), in points, is in centimetres, or None if macOS can't
    say or what it says can't be right.

    The screen measured is the one under the page's middle: usually the Mac's own, and the main display. Its size comes
    from the display itself, so a display that doesn't report one has none, and a page that would come out narrower or
    wider than PAGE_WIDTH_CM means the size was wrong.
    """
    try:
        size = _screen_size(left + width / 2, top + height / 2)
        per_point = None if size is None else cm_per_point(*size)
    except Exception:  # noqa: BLE001 - no window server, a macOS that has changed, or anything else: the fit then
        return None  # does without allowing for the head's turn, as it did before
    if per_point is None:
        return None
    centimetres = width * per_point
    return centimetres if PAGE_WIDTH_CM[0] <= centimetres <= PAGE_WIDTH_CM[1] else None


def window_frame(pid: int, width: int, height: int) -> tuple[float, float, float, float] | None:
    """Where that window is now, or None if macOS can't say."""
    try:
        return pick_window(list_windows(), pid, width, height)
    except Exception:  # noqa: BLE001 - no window server, or a macOS that has changed its names, or anything else:
        return None  # the caller is mid-command, and no window means "use the selection"
