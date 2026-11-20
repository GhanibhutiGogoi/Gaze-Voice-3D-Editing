"""Windows generic credentials, accessed in-process so secrets never enter argv."""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _api():
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _target(account: str) -> str:
    return "GazeVoice/" + account


def read(account: str) -> str | None:
    api, pointer = _api(), ctypes.POINTER(Credential)()
    if not api.CredReadW(_target(account), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = pointer.contents
        return ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-16le")
    finally:
        api.CredFree(pointer)


def write(account: str, key: str) -> None:
    blob = key.encode("utf-16le")
    if len(blob) > 2560:
        raise ValueError("The API key exceeds Windows Credential Manager's 2560-byte limit.")
    data = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = Credential(Type=1, TargetName=_target(account), CredentialBlobSize=len(blob),
                            CredentialBlob=data, Persist=2, UserName=account)
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete(account: str) -> None:
    if not _api().CredDeleteW(_target(account), 1, 0) and ctypes.get_last_error() != 1168:
        raise ctypes.WinError(ctypes.get_last_error())
