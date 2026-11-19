"""Open a scan input without following Windows file reparse points."""
import ctypes
import errno
import msvcrt
import os
from ctypes import wintypes as w


class _Tag(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]


def open_regular(path):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.CreateFileW(path, 0x80000000, 7, None, 3, 0x00200000, None)  # OPEN_REPARSE_POINT
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        tag = _Tag()
        if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise ctypes.WinError(ctypes.get_last_error())
        if tag.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise OSError(errno.ELOOP, "reparse point was not followed", path)
        if tag.attributes & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
            raise OSError(errno.EISDIR, "not a regular file", path)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    return fd  # ownership passes to os.close / os.fdopen
